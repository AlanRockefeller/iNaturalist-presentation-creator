"""Rare finds: the rarest species in an iNaturalist search, by how many
observations iNaturalist has of each worldwide.

A "rare" source is an observations search plus a few settings
(``models.RareSource``). It is searched only when the user asks (when it is
added, or with Search again), never on an ordinary refresh:

1. ``scan_species``: the taxa in the search, rarest in the search first
   (``/v2/observations/species_counts``, ``order=asc``, 500 a page, at most 20
   pages, so at most 10,000 taxa and never past iNaturalist's paging ceiling).
   Only species and finer ranks are kept: a count for a whole genus says
   nothing about rarity.
2. ``rank_species``: live worldwide counts for the likely rarest, re-counted
   until the top N are all live counts. A taxon's cached
   ``observations_count`` can be badly stale (a renamed taxon can read 0), so
   it only decides which ones to re-count.
3. ``pick_observations``: the best observations of each chosen species within
   the search (research grade first, then the most favorites), up to
   ``per_taxon`` each, with photos (and, if asked, a photo licensed for
   commercial use). They are offered for review like any new observations.

A species that has observed varieties or forms gets no row of its own from
species_counts, so it is counted on its own (with its descendants), as the old
dikarya.us Rare Finds page did.

Every later load re-counts the species a project holds from rare sources
(``counts_for``: worldwide and within the search, one request per 200 species
each, plus one per species with observed varieties). Counts are derived data:
they live on each observation in the workspace (``obs["rare"]``) and are never
written to the project file. Every request goes through ``INatClient`` (rate
limiter, budgets, per-client share); ``estimate_requests`` bounds a search.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Callable, Iterable

from .inaturalist import ID_BATCH, PER_PAGE, SPECIES_PER_PAGE, INatClient, INatError
from .models import DEFAULT_LICENSES, RareSource

MAX_SCAN_PAGES = 20      # 20 x 500 taxa = 10,000, iNaturalist's paging ceiling
MAX_COUNT_ROUNDS = 10    # re-count rounds before the ranking stands as it is
SPECIES_RANK_LEVEL = 10  # species, hybrids and anything finer
TAXON_BATCH = 100        # species per observations request
OBS_PAGES = 5            # pages of observations read for one batch of species
COMMERCIAL_LICENSES = frozenset(DEFAULT_LICENSES)  # allow commercial use

# progress(phase, done, total): phase is "scan", "count" or "photos".
ProgressFn = Callable[[str, int, int], None]


def is_provisional(name: str) -> bool:
    """A provisional name, such as Inocybe sp. 'PNW77' or one marked nom. prov."""
    return "'" in name or '"' in name or "nom. prov" in name.lower()


def estimate_requests(top: int) -> int:
    """The most requests one search of ``top`` species can make: the scan, the
    re-count rounds, one count per species with observed varieties, and the
    observations (a batch's pages, or one request per species)."""
    batches = math.ceil(max(1, top) / TAXON_BATCH)
    return MAX_SCAN_PAGES + MAX_COUNT_ROUNDS + top + max(batches * OBS_PAGES, top)


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _check(cancelled: Callable[[], bool] | None) -> None:
    if cancelled and cancelled():
        raise INatError("Cancelled.")


def _rarest_first(world: Callable[[dict], int]):
    return lambda c: (world(c), -c["local"], c["name"].casefold(), c["id"])


# ---------------------------------------------------------------------------
# 1. The species in the search
# ---------------------------------------------------------------------------

def scan_species(client: INatClient, params: dict[str, str], progress: ProgressFn | None = None,
                 cancelled: Callable[[], bool] | None = None) -> tuple[list[dict], int]:
    """Species-rank (or finer) taxa in the search, rarest in the search first:
    [{id, name, local, cached}], and how many taxa of any rank the search has."""
    out: list[dict] = []
    total = None
    pages = 1
    page = 1
    while page <= pages:
        _check(cancelled)
        rows, count = client.species_counts({**params, "order": "asc"}, page, SPECIES_PER_PAGE)
        if total is None:
            total = count
            pages = min(MAX_SCAN_PAGES, math.ceil(count / SPECIES_PER_PAGE))
        if progress:
            progress("scan", page, pages)
        for row in rows:
            taxon = row.get("taxon") or {}
            tid, level = taxon.get("id"), taxon.get("rank_level")
            if not isinstance(tid, int) or not isinstance(level, (int, float)) or level > SPECIES_RANK_LEVEL:
                continue
            out.append({"id": tid, "name": (taxon.get("name") or f"Taxon {tid}").strip(),
                        "local": int(row.get("count") or 0), "cached": int(taxon.get("observations_count") or 0)})
        if len(rows) < SPECIES_PER_PAGE:
            break
        page += 1
    return out, total or 0


# ---------------------------------------------------------------------------
# 2. Live counts and the ranking
# ---------------------------------------------------------------------------

def live_counts(client: INatClient, taxon_ids: Iterable[int], scope: dict[str, str] | None = None,
                cancelled: Callable[[], bool] | None = None) -> dict[int, int]:
    """Observations of each taxon: worldwide (``scope`` None, any quality
    grade) or within a search (``scope`` its parameters). A taxon missing from
    species_counts' answer (it has observed descendants) is counted on its own."""
    want = list(dict.fromkeys(int(t) for t in taxon_ids))
    out: dict[int, int] = {}
    for start in range(0, len(want), ID_BATCH):
        batch = want[start:start + ID_BATCH]
        wanted = set(batch)
        _check(cancelled)
        rows, _ = client.species_counts({**(scope or {}), "taxon_id": ",".join(str(t) for t in batch)})
        for row in rows:
            tid = (row.get("taxon") or {}).get("id")
            if tid in wanted:
                out[tid] = int(row.get("count") or 0)
        for tid in batch:
            if tid not in out:
                _check(cancelled)
                out[tid] = client.count_observations({**(scope or {}), "taxon_id": str(tid)})
    return out


def rank_species(client: INatClient, candidates: list[dict], top: int, described_only: bool = False,
                 progress: ProgressFn | None = None, cancelled: Callable[[], bool] | None = None) -> list[dict]:
    """The ``top`` rarest worldwide, rarest first, each with a live ``world``
    count. Re-counts the likely rarest until the top N are all live."""
    pool = [c for c in candidates if not (described_only and is_provisional(c["name"]))]
    live: dict[int, int] = {}
    order = _rarest_first(lambda c: live.get(c["id"], c["cached"]))
    for round_number in range(1, MAX_COUNT_ROUNDS + 1):
        pool.sort(key=order)
        need = [c["id"] for c in pool[:top] if c["id"] not in live]
        if not need:
            break
        if progress:
            progress("count", round_number, MAX_COUNT_ROUNDS)
        live.update(live_counts(client, need, cancelled=cancelled))
    pool.sort(key=order)
    chosen = pool[:top]
    rest = [c["id"] for c in chosen if c["id"] not in live]
    if rest:  # the rounds ran out: count what is left, so every count shown is live
        live.update(live_counts(client, rest, cancelled=cancelled))
        chosen.sort(key=order)
    return [{**c, "world": live[c["id"]]} for c in chosen]


# ---------------------------------------------------------------------------
# 3. Observations of the chosen species
# ---------------------------------------------------------------------------

def _bucket(raw: dict, wanted: set[int]) -> int | None:
    """The chosen taxon this observation is of (itself or an ancestor)."""
    taxon = raw.get("taxon") or {}
    chain = [a for a in (taxon.get("ancestor_ids") or []) if isinstance(a, int)]
    if isinstance(taxon.get("id"), int) and taxon["id"] not in chain:
        chain.append(taxon["id"])
    for tid in reversed(chain):
        if tid in wanted:
            return tid
    return None


def _usable(raw: dict, commercial_only: bool) -> bool:
    photos = [p for p in raw.get("photos") or [] if isinstance(p, dict) and not p.get("hidden")]
    if not commercial_only:
        return bool(photos)
    return any((p.get("license_code") or "arr") in COMMERCIAL_LICENSES for p in photos)


def _best_first(raw: dict) -> tuple:
    return (raw.get("quality_grade") != "research", -int(raw.get("faves_count") or 0), raw.get("id") or 0)


def pick_observations(client: INatClient, params: dict[str, str], species: list[dict], per_taxon: int,
                      commercial_only: bool = False, progress: ProgressFn | None = None,
                      cancelled: Callable[[], bool] | None = None) -> list[dict]:
    """Raw observations within the search: up to ``per_taxon`` per species, best
    first, in the order of ``species``. A batch whose observations fit in a few
    pages is read whole; otherwise each species gets one request of its own."""
    order = [s["id"] for s in species]
    wanted = set(order)
    local = {s["id"]: int(s.get("local") or 0) for s in species}
    found: dict[int, list[dict]] = {t: [] for t in order}
    seen: set[int] = set()

    def take(results: list[dict]) -> None:
        for raw in results:
            rid = raw.get("id")
            if not isinstance(rid, int) or rid in seen:
                continue
            seen.add(rid)
            tid = _bucket(raw, wanted)
            if tid is not None and _usable(raw, commercial_only):
                found[tid].append(raw)

    batches = [order[i:i + TAXON_BATCH] for i in range(0, len(order), TAXON_BATCH)]
    for n, batch in enumerate(batches, 1):
        if progress:
            progress("photos", n, len(batches))
        query = {**params, "taxon_id": ",".join(str(t) for t in batch), "order_by": "votes", "order": "desc"}
        if sum(local.get(t, 0) for t in batch) <= OBS_PAGES * PER_PAGE:
            page = 1
            while True:
                _check(cancelled)
                results, total = client.search_page(query, page, PER_PAGE)
                take(results)
                if not results or page * PER_PAGE >= min(total, OBS_PAGES * PER_PAGE):
                    break
                page += 1
        else:
            for tid in batch:
                _check(cancelled)
                results, _ = client.search_page({**query, "taxon_id": str(tid)}, 1, min(PER_PAGE, 50))
                take(results)
    out: list[dict] = []
    for tid in order:
        out.extend(sorted(found[tid], key=_best_first)[:per_taxon])
    return out


# ---------------------------------------------------------------------------
# The whole search, and the counts on later loads
# ---------------------------------------------------------------------------

def search(client: INatClient, params: dict[str, str], rare: RareSource, progress: ProgressFn | None = None,
           cancelled: Callable[[], bool] | None = None) -> dict:
    """{"raws": observations to offer, rarest species first; "species": the
    chosen species [{id, name, world, local}]; "taxa": how many taxa the search
    has; "capped": True if it has more than one scan reads}."""
    candidates, taxa = scan_species(client, params, progress, cancelled)
    species = rank_species(client, candidates, rare.top, rare.described_only, progress, cancelled) if candidates else []
    raws = pick_observations(client, params, species, rare.per_taxon, rare.commercial_only, progress, cancelled) \
        if species else []
    return {"raws": raws, "species": species, "taxa": taxa, "capped": taxa > MAX_SCAN_PAGES * SPECIES_PER_PAGE}


def counts_for(client: INatClient, params: dict[str, str], taxon_ids: Iterable[int],
               cancelled: Callable[[], bool] | None = None) -> dict[int, tuple[int, int]]:
    """(worldwide, in the search) observations of each taxon."""
    ids = list(dict.fromkeys(int(t) for t in taxon_ids))
    if not ids:
        return {}
    world = live_counts(client, ids, cancelled=cancelled)
    local = live_counts(client, ids, scope=params, cancelled=cancelled)
    return {t: (world.get(t, 0), local.get(t, 0)) for t in ids}


def mark(observations: dict[int, dict], obs_ids: Iterable[int], source_id: str,
         counts: dict[int, tuple[int, int]], counted_at: str) -> None:
    """Put the counts on each observation (``obs["rare"]``), by its own taxon.
    An observation two rare sources found keeps the first one's; one this
    source already counted is updated."""
    for oid in obs_ids:
        obs = observations.get(oid)
        taxon = (obs or {}).get("taxon") or {}
        if not obs or taxon.get("id") not in counts:
            continue
        if obs.get("rare") and obs["rare"].get("source") != source_id:
            continue
        world, local = counts[taxon["id"]]
        obs["rare"] = {"source": source_id, "world": world, "local": local, "counted_at": counted_at}


__all__ = [
    "COMMERCIAL_LICENSES", "counts_for", "estimate_requests", "is_provisional", "live_counts", "mark",
    "pick_observations", "rank_species", "scan_species", "search", "today",
]
