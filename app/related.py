"""More photos of species already in a project: "More of mine" and "More from others".

A project can have up to two "related" sources, one per kind. Each remembers
the species searched so far (``Source.related.taxon_ids``). How to search is
in ``PresentationSettings.related``: the presenter's login, how many per
species, a minimum number of favorites, and for other people's photos the
allowed licenses and whether only research grade observations count.

These sources are searched only when the user asks (a card's button, "Search
all listed species" or "Search again"), never on an ordinary refresh, because
other people's photos cost about one request per species.

API use: every request goes through ``INatClient`` (rate limiter, budgets).
Species are searched 100 at a time, most-favorited first. When the matches of
a batch fit in no more pages than the batch has species, the batch is paged
through (stopping early once favorites fall below the minimum); otherwise each
species gets one request for its top N. So a search never costs more than
``estimate_requests()``, one species is one request, and nothing pages past
10,000 results. A per-species request asks for only the N it keeps rather than
200: it is one page either way, and it is far fewer bytes for iNaturalist.
"""

from __future__ import annotations

import math
from typing import Callable, Iterable

from .inaturalist import PER_PAGE, INatClient, INatError, SourceError, canonical_url
from .models import RelatedSettings, Source

LICENSE_LABELS = {
    "cc0": "CC0", "cc-by": "CC BY", "cc-by-sa": "CC BY-SA", "cc-by-nd": "CC BY-ND",
    "cc-by-nc": "CC BY-NC", "cc-by-nc-sa": "CC BY-NC-SA", "cc-by-nc-nd": "CC BY-NC-ND",
    "arr": "All rights reserved",
}
ALL_LICENSES = set(LICENSE_LABELS)
LABELS = {"mine": "More of my photos", "others": "Other people's photos"}
TAXON_BATCH = 100
MAX_RESULTS = 10_000  # iNaturalist's page * per_page ceiling
SPECIES_RANK_LEVEL = 10  # species, hybrids and anything finer
# When licenses are checked here rather than by iNaturalist, a species may
# need a few pages of 200 before N photos with an allowed license turn up.
LOCAL_FILTER_PAGES = 3


class SearchCancelled(INatError):
    """The user cancelled the search."""

    def __init__(self):
        super().__init__("Cancelled.")


def limits(whose: str, rs: RelatedSettings) -> tuple[int, int]:
    """(up to N per species, minimum favorites) for this kind of search."""
    opts = rs.mine if whose == "mine" else rs.others
    return opts.per_taxon, opts.min_faves


def licenses_filtered_here(rs: RelatedSettings) -> bool:
    """iNaturalist cannot filter for "all rights reserved", so with that and
    only some other licenses ticked the licenses are checked here instead."""
    chosen = set(rs.others.licenses)
    return "arr" in chosen and chosen != ALL_LICENSES


def api_filters(whose: str, rs: RelatedSettings, strict: bool = True) -> dict[str, str]:
    """Search parameters other than taxon_id. ``strict`` refuses a "mine"
    search without a username (a link can still be built without one)."""
    login = rs.username
    _per_taxon, min_faves = limits(whose, rs)
    params: dict[str, str] = {}
    if whose == "mine":
        if login:
            params["user_id"] = login
        elif strict:
            raise SourceError("Enter your iNaturalist username in More photos (Select & Organize) to search your own photos.")
    else:
        if login:
            params["not_user_id"] = login
        if rs.others.research_only:
            params["quality_grade"] = "research"
        if "arr" not in rs.others.licenses:
            params["photo_license"] = ",".join(c for c in LICENSE_LABELS if c in rs.others.licenses)
    params["order_by"] = "votes"  # favorites
    params["order"] = "desc"
    if min_faves >= 1:
        params["popular"] = "true"  # favorited at least once
    return params


def related_fields(source: Source, rs: RelatedSettings) -> dict:
    """The default label and the iNaturalist link for a related source."""
    whose = source.related.whose
    params = api_filters(whose, rs, strict=False)
    if source.related.taxon_ids:
        params["taxon_id"] = ",".join(str(t) for t in source.related.taxon_ids)
    return {"label": LABELS[whose], "url": canonical_url(params)}


def photo_allowed(photo: dict, licenses: Iterable[str]) -> bool:
    """Whether a photo (raw API or normalized) has one of ``licenses``."""
    code = photo.get("license_code", photo.get("license")) or "arr"
    return code in licenses


def pages_per_species(whose: str, rs: RelatedSettings) -> int:
    """The most pages one species' own search can take."""
    return LOCAL_FILTER_PAGES if whose == "others" and licenses_filtered_here(rs) else 1


def estimate_requests(n_taxa: int, pages: int = 1) -> int:
    """The most requests a search of ``n_taxa`` species can make: per batch, its
    first page plus at most ``pages`` per species (one species: ``pages``).
    Mirrored by estimateRequests() in app.js."""
    total = 0
    for start in range(0, max(0, n_taxa), TAXON_BATCH):
        size = min(TAXON_BATCH, n_taxa - start)
        total += 1 + size * pages if size > 1 else pages
    return total


def _bucket(raw: dict, wanted: set[int]) -> int | None:
    """The most specific searched species this observation is of (a variety
    counts under its species when only the species was searched)."""
    taxon = raw.get("taxon") or {}
    chain = [a for a in (taxon.get("ancestor_ids") or []) if isinstance(a, int)]
    if isinstance(taxon.get("id"), int) and taxon["id"] not in chain:
        chain.append(taxon["id"])
    for tid in reversed(chain):
        if tid in wanted:
            return tid
    return None


def run_related_search(
    client: INatClient,
    whose: str,
    taxon_ids: list[int],
    rs: RelatedSettings,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    exclude: Iterable[str] = (),
    max_photos: int = 200,
) -> list[dict]:
    """Raw observations: up to N per species, most-favorited first, in the
    order of ``taxon_ids``. ``exclude``: the presenter's logins (own_logins()),
    left out of "others". Only an observation's first ``max_photos`` photos
    count, as only those are kept (normalize_observation()).

    Raises SourceError for a "mine" search with no username, and
    SearchCancelled when ``cancelled()`` turns true."""
    filters = api_filters(whose, rs)
    per_taxon, min_faves = limits(whose, rs)
    excluded = {login.lower() for login in exclude} | ({rs.username.lower()} if rs.username else set())
    licenses = set(rs.others.licenses)
    check_licenses = whose == "others"
    filter_here = check_licenses and licenses_filtered_here(rs)
    fetch_n = PER_PAGE if filter_here else per_taxon
    pages_each = pages_per_species(whose, rs)
    want = list(dict.fromkeys(int(t) for t in taxon_ids))
    wanted = set(want)
    kept: dict[int, list[dict]] = {t: [] for t in want}
    seen: set[int] = set()
    estimate = estimate_requests(len(want), pages_each)
    made = 0

    def acceptable(raw: dict) -> bool:
        if int(raw.get("faves_count") or 0) < min_faves:
            return False
        if whose == "others" and ((raw.get("user") or {}).get("login") or "").lower() in excluded:
            return False
        photos = [p for p in raw.get("photos") or [] if isinstance(p, dict) and not p.get("hidden")][:max_photos]
        if not photos:
            return False
        return not check_licenses or any(photo_allowed(p, licenses) for p in photos)

    def take(results: list[dict]) -> None:
        for raw in results:
            rid = raw.get("id")
            if not isinstance(rid, int) or rid in seen:
                continue
            seen.add(rid)
            tid = _bucket(raw, wanted)
            if tid is not None and len(kept[tid]) < per_taxon and acceptable(raw):
                kept[tid].append(raw)

    def page(params: dict, number: int, per_page: int) -> tuple[list[dict], int]:
        nonlocal made
        if cancelled and cancelled():
            raise SearchCancelled()
        results, total = client.search_page(params, number, per_page)
        made += 1
        if progress:
            progress(made, estimate)
        return results, total

    def below_minimum(results: list[dict]) -> bool:
        return bool(results) and int(results[-1].get("faves_count") or 0) < min_faves

    def one_species(tid: int, params: dict) -> None:
        """Its own top N: one page, or a few when licenses are checked here."""
        number = 0
        while number < pages_each and (number + 1) * fetch_n <= MAX_RESULTS:
            number += 1
            results, total = page(params, number, fetch_n)
            take(results)
            if (len(kept[tid]) >= per_taxon or len(results) < fetch_n or below_minimum(results)
                    or number * fetch_n >= total):
                break

    for start in range(0, len(want), TAXON_BATCH):
        batch = want[start:start + TAXON_BATCH]
        params = {**filters, "taxon_id": ",".join(str(t) for t in batch)}
        if len(batch) == 1:  # its own top N is the answer
            one_species(batch[0], params)
            continue
        results, total = page(params, 1, PER_PAGE)
        take(results)
        pages = math.ceil(min(total, MAX_RESULTS) / PER_PAGE)
        if total <= MAX_RESULTS and pages <= len(batch):
            number = 1
            while (number < pages and results and not below_minimum(results)
                   and not all(len(kept[t]) >= per_taxon for t in batch)):
                number += 1
                results, _ = page(params, number, PER_PAGE)
                take(results)
        else:
            for tid in batch:
                if len(kept[tid]) >= per_taxon:
                    continue  # filled by the batch's first page
                one_species(tid, {**filters, "taxon_id": str(tid)})

    out: list[dict] = []
    for tid in want:
        out.extend(sorted(kept[tid], key=lambda r: -int(r.get("faves_count") or 0)))
    return out


def is_species(obs: dict) -> bool:
    """Identified to species or finer: only those can be searched for more photos."""
    taxon = obs.get("taxon") or {}
    level = taxon.get("rank_level")
    return bool(taxon.get("id")) and isinstance(level, (int, float)) and level <= SPECIES_RANK_LEVEL


__all__ = [
    "LABELS", "LICENSE_LABELS", "SearchCancelled", "api_filters", "estimate_requests", "is_species",
    "licenses_filtered_here", "pages_per_species", "photo_allowed", "related_fields", "run_related_search",
]
