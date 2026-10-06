"""iNaturalist integration: source parsing, a rate-limited API client, normalization.

Security model: the server never fetches a URL a browser gave it. A source URL
is *parsed* into observation-search parameters, and only
``https://api.inaturalist.org`` is ever contacted with them. Photo URLs come from
API responses and are re-validated against a host/path allowlist before use.

API limits (https://www.inaturalist.org/pages/api+recommended+practices):
* every call goes through one process-wide ~1 req/s limiter and a daily budget;
* searches use per_page=200 and the v2 ``fields`` parameter so responses carry
  only what this app uses;
* lookups by id are batched 200 at a time;
* searches over 10,000 results are refused (that is a bulk export, not an API job);
* 429/5xx are retried with backoff that respects Retry-After;
* requests carry a descriptive User-Agent.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx

from .config import Settings
from .ratelimit import BudgetExceeded, DailyBudget, RateLimiter

log = logging.getLogger(__name__)

LIFE_TAXON_ID = 48460
PER_PAGE = 200
ID_BATCH = 200
GROUP_RANKS = ("kingdom", "phylum", "class", "order", "family", "genus")

CANONICAL_HOST = "www.inaturalist.org"

# iNaturalist Network sites all share one database and URL scheme.
INAT_NETWORK_HOSTS = {
    "inaturalist.org", "inaturalist.ca", "inaturalist.nz", "naturalista.mx",
    "biodiversity4all.org", "argentinat.org", "inaturalist.ala.org.au",
    "inaturalist.lu", "israelinat.org", "inaturalist.laji.fi", "inaturalist.se",
    "naturalista.uy", "inaturalist.mma.gob.cl", "inaturalist.ge", "inaturalist.lu",
    "naturalista.co", "inaturalist.ie", "inaturalist.co.uk",
}

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,39}$")
PARAM_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_INPUT_LEN = 2000
MAX_PARAMS = 40
MAX_PARAM_VALUE_LEN = 500

# Website-only or paging parameters that must not reach the API from a URL.
DROPPED_PARAMS = {
    "page", "per_page", "fields", "only_id", "subview", "view", "ttl", "callback",
    "locale", "preferred_place_id", "return_bounds", "utf8", "x", "y", "z",
    "photos_view", "map_type", "skip_total", "id_above", "id_below",
}
ALLOWED_ORDER_BY = {"created_at", "id", "observed_on", "species_guess", "votes", "updated_at"}

OBSERVATION_FIELDS = (
    "(id:!t,uuid:!t,faves_count:!t,observed_on:!t,created_at:!t,place_guess:!t,"
    "quality_grade:!t,obscured:!t,"
    "user:(login:!t,name:!t),"
    "taxon:(id:!t,name:!t,rank:!t,rank_level:!t,preferred_common_name:!t,ancestor_ids:!t),"
    "photos:(id:!t,url:!t,hidden:!t,license_code:!t,attribution:!t,original_dimensions:(width:!t,height:!t)),"
    "ofvs:(name:!t,value:!t))"
)

PHOTO_HOSTS = {"inaturalist-open-data.s3.amazonaws.com", "static.inaturalist.org"}
PHOTO_PATH_RE = re.compile(
    r"^/photos/(\d{1,12})/(square|thumb|small|medium|large|original)\.(jpe?g|png|gif|webp)$",
    re.IGNORECASE,
)
PHOTO_SIZES = {"square", "thumb", "small", "medium", "large", "original"}


class SourceError(ValueError):
    """Invalid source input. The message is safe to show to users."""


class INatError(Exception):
    """An iNaturalist request failed. The message is safe to show to users."""


class TooManyResults(INatError):
    def __init__(self, total: int, limit: int, label: str = ""):
        self.total, self.limit = total, limit
        where = f' "{label}"' if label else ""
        super().__init__(
            f"The source{where} matches {total:,} observations; the limit is {limit:,}. "
            "Add filters to the iNaturalist URL (for example a taxon, place, date range "
            "or quality grade) to narrow it down."
        )


# ---------------------------------------------------------------------------
# Source parsing
# ---------------------------------------------------------------------------

def _is_inat_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if host in INAT_NETWORK_HOSTS:
        return True
    return host.endswith(".inaturalist.org") and re.fullmatch(r"[a-z0-9.-]+", host) is not None


def username_url(username: str) -> str:
    return f"https://{CANONICAL_HOST}/observations?{urlencode({'user_id': username})}"


def canonical_url(params: dict[str, str]) -> str:
    query = urlencode(sorted(params.items()), quote_via=quote, safe=",:")
    return f"https://{CANONICAL_HOST}/observations" + (f"?{query}" if query else "")


def _clean_params(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    merged: dict[str, list[str]] = {}
    count = 0
    for key, value in pairs:
        key = key.strip().lower()
        if key.endswith("[]"):
            key = key[:-2]
        if not key or key in DROPPED_PARAMS:
            continue
        if not PARAM_KEY_RE.match(key):
            raise SourceError(f"Unsupported URL parameter: {key[:40]!r}")
        value = value.strip()
        if any(ord(c) < 32 for c in value):
            raise SourceError("URL parameters may not contain control characters.")
        if len(value) > MAX_PARAM_VALUE_LEN:
            raise SourceError(f"The value of {key!r} is too long.")
        count += 1
        if count > MAX_PARAMS:
            raise SourceError("That URL has too many parameters.")
        merged.setdefault(key, []).append(value)
    params: dict[str, str] = {}
    for key, values in merged.items():
        # The website writes "any" to mean "no filter" (verifiable=any,
        # quality_grade=any, place_id=any); the API rejects it, so drop it.
        values = [v for v in values if v != "" and v.lower() != "any"]
        if not values:
            continue
        params[key] = ",".join(dict.fromkeys(values))
    if "order_by" in params and params["order_by"] not in ALLOWED_ORDER_BY:
        # e.g. order_by=random would make pagination unstable
        params.pop("order_by")
    if "order" in params and params["order"] not in ("asc", "desc"):
        params.pop("order")
    return params


def parse_source_input(raw: str) -> dict:
    """Turn a username or iNaturalist observations URL into a source description.

    Returns ``{"type", "input", "username", "url", "params", "label"}``.
    Raises SourceError with a user-facing message.
    """
    if raw is None:
        raise SourceError("Enter an iNaturalist username or observations URL.")
    text = raw.strip()
    if not text:
        raise SourceError("Enter an iNaturalist username or observations URL.")
    if len(text) > MAX_INPUT_LEN:
        raise SourceError("That input is too long.")
    if any(ord(c) < 32 for c in text):
        raise SourceError("That input contains control characters.")

    candidate = text[1:] if text.startswith("@") else text
    if USERNAME_RE.match(candidate) and not candidate.isdigit():
        return {
            "type": "username",
            "input": text,
            "username": candidate,
            "url": username_url(candidate),
            "params": {"user_id": candidate},
            "label": candidate,
        }

    url_text = text
    if "://" not in url_text:
        if re.match(r"^(www\.)?[a-z0-9.-]+\.[a-z]{2,}(/|$)", url_text, re.IGNORECASE):
            url_text = "https://" + url_text
        else:
            raise SourceError(
                "Enter an iNaturalist username (like alan_rockefeller) or a link to an "
                "iNaturalist observations search."
            )
    try:
        parts = urlsplit(url_text)
    except ValueError as exc:
        raise SourceError("That does not look like a valid URL.") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise SourceError("Only http(s) iNaturalist links are supported.")
    if parts.username or parts.password:
        raise SourceError("URLs with embedded credentials are not allowed.")
    try:
        port = parts.port
    except ValueError as exc:
        raise SourceError("That URL has an invalid port.") from exc
    if port not in (None, 80, 443):
        raise SourceError("That URL is not an iNaturalist observations link.")
    host = (parts.hostname or "").lower()
    if not host or not _is_inat_host(host):
        raise SourceError("Only iNaturalist (or iNaturalist Network) links are supported.")

    path = re.sub(r"/+", "/", parts.path or "/").rstrip("/") or "/"
    path = re.sub(r"\.(html?|json)$", "", path)
    segments = [s for s in path.split("/") if s]
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False)
    except ValueError as exc:
        raise SourceError("That URL's query string could not be read.") from exc

    url_username = None
    extra: list[tuple[str, str]] = []
    if not segments:
        raise SourceError("Link to an iNaturalist observations search, e.g. https://www.inaturalist.org/observations?taxon_id=47170")
    head = segments[0].lower()
    if head == "observations":
        if len(segments) == 1 or (len(segments) == 2 and segments[1].lower() in ("identify", "export")):
            pass
        elif len(segments) == 2 and segments[1].isdigit():
            extra.append(("id", segments[1]))
        elif len(segments) == 2 and USERNAME_RE.match(segments[1]):
            url_username = segments[1]
        else:
            raise SourceError("That iNaturalist link is not an observations search.")
    elif head == "people" and len(segments) == 2 and USERNAME_RE.match(segments[1]) and not segments[1].isdigit():
        url_username = segments[1]
    elif head == "projects" and len(segments) == 2 and re.fullmatch(r"[A-Za-z0-9_\-]{1,100}", segments[1]):
        extra.append(("project_id", segments[1]))
    else:
        raise SourceError("That iNaturalist link is not an observations search.")

    params = _clean_params(list(pairs) + extra)
    if url_username and not params:
        # /observations/<login> or /people/<login> with no other filters is a username source.
        return {
            "type": "username",
            "input": text,
            "username": url_username,
            "url": username_url(url_username),
            "params": {"user_id": url_username},
            "label": url_username,
        }
    if url_username:
        params["user_id"] = url_username
    if not params:
        raise SourceError(
            "That link has no filters and would match every observation on iNaturalist. "
            "Add a taxon, place, user, project or date filter."
        )
    return {
        "type": "url",
        "input": text,
        "username": None,
        "url": canonical_url(params),
        "params": params,
        "label": default_label(params),
    }


def params_from_canonical_url(url: str) -> dict[str, str]:
    """Re-derive API parameters from a saved project's canonical URL (re-validated)."""
    return parse_source_input(url)["params"]


def default_label(params: dict[str, str], username: str | None = None) -> str:
    bits = []
    for key in ("user_id", "taxon_name", "taxon_id", "place_id", "project_id", "d1", "d2", "year", "month", "quality_grade"):
        if key in params and not (username and key == "user_id"):
            bits.append(f"{key}={params[key]}")
    label = ", ".join(bits[:3])
    if username:
        label = f"{username}: {label}" if label else username
    return (label or "iNaturalist search")[:80]


def build_source(url: str, username: str | None = None) -> dict:
    """A source from an observations URL plus an optional iNaturalist username.

    The URL is required. A username restricts the search to that person's
    observations and marks the source as the presenter's own photos (type
    "username"), which turns the observer credit off by default. Returns the
    same shape as ``parse_source_input``.
    """
    username = (username or "").strip().lstrip("@")
    if username and (not USERNAME_RE.match(username) or username.isdigit()):
        raise SourceError("That is not a valid iNaturalist username.")
    if not (url or "").strip():
        raise SourceError(
            "Enter an iNaturalist observations URL. Filter the observations on iNaturalist "
            "first, then copy the link from the address bar."
        )
    parsed = parse_source_input(url)
    if parsed["type"] == "username" and "/" not in url:
        raise SourceError(
            "That looks like a username, not a URL. Put it in the username box, and paste "
            "the link to a filtered iNaturalist observations search in the URL box."
        )
    if not username:
        return parsed
    params = dict(parsed["params"])
    existing = params.get("user_id") or params.get("user_login")
    if existing and existing.lower() != username.lower():
        raise SourceError(
            f"That URL is for the user \"{existing}\" but the username you entered is \"{username}\"."
        )
    params.pop("user_login", None)
    params["user_id"] = username
    return {
        "type": "username",
        "input": parsed["input"],
        "username": username,
        "url": canonical_url(params),
        "params": params,
        "label": default_label(params, username),
    }


def source_params(source) -> dict[str, str]:
    """API search parameters for a saved source (re-validated from its URL)."""
    params = dict(parse_source_input(source.url)["params"])
    if source.type == "username" and source.username:
        if not USERNAME_RE.match(source.username):
            raise SourceError("A saved source has an invalid username.")
        params.pop("user_login", None)
        params["user_id"] = source.username
    return params


def api_search_params(params: dict[str, str]) -> dict[str, str]:
    out = dict(params)
    out.setdefault("photos", "true")
    out["locale"] = "en"
    out["per_page"] = str(PER_PAGE)
    out["fields"] = OBSERVATION_FIELDS
    return out


# ---------------------------------------------------------------------------
# Photo URLs
# ---------------------------------------------------------------------------

def photo_url(url: str, size: str) -> str | None:
    """Return the iNat URL for ``size`` of a photo, or None if ``url`` is not trusted."""
    if size not in PHOTO_SIZES or not isinstance(url, str) or len(url) > 500:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or (parts.hostname or "").lower() not in PHOTO_HOSTS:
        return None
    if parts.username or parts.password or parts.port not in (None, 443):
        return None
    m = PHOTO_PATH_RE.match(parts.path)
    if not m:
        return None
    return f"https://{parts.hostname.lower()}/photos/{m.group(1)}/{size}.{m.group(3)}"


# ---------------------------------------------------------------------------
# Name formatting and normalization
# ---------------------------------------------------------------------------

INFRA_PREFIX = {"subspecies": "subsp.", "variety": "var.", "form": "f."}
GENUS_SUBDIVISIONS = {"subgenus": "subg.", "section": "sect.", "subsection": "subsect.", "series": "ser."}


def format_taxon_name(taxon: dict | None, ranks: dict | None = None) -> str:
    """The scientific display text for a taxon; "" for no taxon or "Life"."""
    if not taxon:
        return ""
    name = (taxon.get("name") or "").strip()
    if not name or taxon.get("id") == LIFE_TAXON_ID or name.lower() == "life":
        return ""
    rank = (taxon.get("rank") or "").lower()
    if rank == "genus":
        return f"{name} sp."
    if rank in INFRA_PREFIX:
        words = name.split()
        if len(words) == 3:
            return f"{words[0]} {words[1]} {INFRA_PREFIX[rank]} {words[2]}"
        return name
    if rank == "complex":
        return f"{name} complex"
    if rank in GENUS_SUBDIVISIONS:
        genus = (ranks or {}).get("genus")
        prefix = GENUS_SUBDIVISIONS[rank]
        return f"{genus} {prefix} {name}" if genus else f"{prefix} {name}"
    return name


def _provisional_name(ofvs) -> str | None:
    for ofv in ofvs or []:
        if not isinstance(ofv, dict):
            continue
        if str(ofv.get("name", "")).strip().lower() == "provisional species name":
            value = str(ofv.get("value") or "").strip()
            if value:
                return value[:200]
    return None


def normalize_observation(raw: dict, taxa: dict[int, dict]) -> dict | None:
    """Reduce an API observation to what the app uses. ``taxa`` maps id -> {name, rank}."""
    try:
        obs_id = int(raw["id"])
    except (KeyError, TypeError, ValueError):
        return None
    taxon_raw = raw.get("taxon") or None
    taxon = None
    ranks: dict[str, str] = {}
    if taxon_raw and taxon_raw.get("id"):
        for anc_id in taxon_raw.get("ancestor_ids") or []:
            info = taxa.get(int(anc_id)) if isinstance(anc_id, int) or str(anc_id).isdigit() else None
            if info and info.get("rank") in GROUP_RANKS:
                ranks[info["rank"]] = info["name"]
        own_rank = (taxon_raw.get("rank") or "").lower()
        if own_rank in GROUP_RANKS and taxon_raw.get("name"):
            ranks[own_rank] = taxon_raw["name"]
        taxon = {
            "id": int(taxon_raw["id"]),
            "name": taxon_raw.get("name") or "",
            "rank": own_rank,
            "rank_level": taxon_raw.get("rank_level"),
        }
    photos = []
    for p in raw.get("photos") or []:
        if not isinstance(p, dict) or p.get("hidden"):
            continue
        url = photo_url(p.get("url") or "", "square")
        if not url or not p.get("id"):
            continue
        dims = p.get("original_dimensions") or {}
        photos.append({
            "id": int(p["id"]),
            "url": url,
            "license": p.get("license_code") or None,
            "attribution": (p.get("attribution") or "")[:300],
            "width": dims.get("width") or None,
            "height": dims.get("height") or None,
        })
    user = raw.get("user") or {}
    common = (taxon_raw or {}).get("preferred_common_name") if taxon else None
    if taxon and (taxon["id"] == LIFE_TAXON_ID):
        common = None
    observed_on = raw.get("observed_on")
    if not (isinstance(observed_on, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", observed_on)):
        observed_on = None
    return {
        "id": obs_id,
        "uri": f"https://www.inaturalist.org/observations/{obs_id}",
        "taxon": taxon,
        "ranks": ranks,
        "inat_name": format_taxon_name(taxon, ranks),
        "common_name": (common or None),
        "observed_on": observed_on,
        "created_at": raw.get("created_at") or None,
        "place_guess": (raw.get("place_guess") or "").strip() or None,
        "faves_count": int(raw.get("faves_count") or 0),
        "quality_grade": raw.get("quality_grade") or None,
        "obscured": bool(raw.get("obscured")),
        "user": {"login": user.get("login") or "", "name": (user.get("name") or "").strip() or None},
        "photos": photos,
        "provisional_name": _provisional_name(raw.get("ofvs")),
    }


def _rejection_message(resp: httpx.Response) -> str:
    """Name the filter iNaturalist refused, when it says which one."""
    try:
        errors = resp.json().get("errors") or []
        path = str(errors[0].get("path") or errors[0].get("instancePath") or "").strip("/")
    except (ValueError, AttributeError, IndexError, TypeError):
        path = ""
    if path and re.fullmatch(r"[A-Za-z0-9_\[\]]{1,40}", path):
        return f'iNaturalist rejected the "{path}" filter in that URL. Remove or change it and try again.'
    return "iNaturalist rejected that search. Check the filters in the URL."


# ---------------------------------------------------------------------------
# Taxa cache (taxon names are public reference data, not user data)
# ---------------------------------------------------------------------------

class TaxaCache:
    TTL = 30 * 24 * 3600

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[int, tuple[str, str, float]] = {}
        self._dirty = False
        if path and path.exists():
            try:
                raw = json.loads(path.read_text())
                now = time.time()
                for k, v in raw.items():
                    if now - v[2] < self.TTL:
                        self._data[int(k)] = (v[0], v[1], v[2])
            except (OSError, ValueError, TypeError, IndexError, AttributeError):
                self._data = {}

    def get_many(self, ids: Iterable[int]) -> tuple[dict[int, dict], list[int]]:
        found, missing = {}, []
        with self._lock:
            for i in ids:
                v = self._data.get(i)
                if v:
                    found[i] = {"name": v[0], "rank": v[1]}
                else:
                    missing.append(i)
        return found, missing

    def put_many(self, items: dict[int, dict]) -> None:
        now = time.time()
        with self._lock:
            for i, info in items.items():
                self._data[int(i)] = (info.get("name") or "", info.get("rank") or "", now)
            self._dirty = True

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            if not self._dirty:
                return
            data = {str(k): list(v) for k, v in self._data.items()}
            self._dirty = False
        from .ratelimit import _atomic_write_json
        try:
            _atomic_write_json(self.path, data)
        except OSError:
            log.warning("event=taxa_cache.save_failed")


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

ProgressFn = Callable[[int, int], None]


class INatClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        budget: DailyBudget | None = None,
        taxa_cache: TaxaCache | None = None,
        sleep=time.sleep,
    ):
        self.settings = settings
        self.http = http or httpx.Client(
            timeout=settings.api_timeout,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            follow_redirects=False,
        )
        self.limiter = limiter or RateLimiter(settings.api_min_interval)
        self.budget = budget or DailyBudget(settings.api_daily_budget)
        self.taxa_cache = taxa_cache or TaxaCache(None)
        self._sleep = sleep

    # -- transport -----------------------------------------------------------

    def _get(self, path: str, params: dict[str, str]) -> dict:
        url = self.settings.api_base + path
        attempt = 0
        while True:
            attempt += 1
            self.budget.consume(1)
            self.limiter.wait()
            try:
                resp = self.http.get(url, params=params, headers={
                    "User-Agent": self.settings.user_agent, "Accept": "application/json"})
            except httpx.HTTPError as exc:
                if attempt <= self.settings.api_max_retries:
                    self.limiter.penalize(min(30.0, 2.0 ** attempt))
                    continue
                log.warning("event=inat.request_failed path=%s error=%s", path, type(exc).__name__)
                raise INatError("Could not reach iNaturalist. Please try again shortly.") from exc
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise INatError("iNaturalist returned an unreadable response.") from exc
            if resp.status_code in (429, 500, 502, 503, 504) and attempt <= self.settings.api_max_retries:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else 0.0
                except ValueError:
                    wait = 0.0
                wait = max(wait, 5.0 * attempt if resp.status_code == 429 else 2.0 ** attempt)
                log.warning("event=inat.retry status=%s wait=%.1f", resp.status_code, wait)
                self.limiter.penalize(min(wait, 120.0))
                continue
            if resp.status_code == 422 or resp.status_code == 400:
                raise INatError(_rejection_message(resp))
            if resp.status_code == 429:
                raise INatError("iNaturalist is rate-limiting requests right now. Please try again in a few minutes.")
            raise INatError(f"iNaturalist returned an error (HTTP {resp.status_code}). Please try again later.")

    # -- searches ------------------------------------------------------------

    def search(
        self,
        params: dict[str, str],
        max_results: int,
        progress: ProgressFn | None = None,
        label: str = "",
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[list[dict], int]:
        """All observations matching ``params``, in iNaturalist's result order."""
        query = api_search_params(params)
        query["page"] = "1"
        first = self._get("/v2/observations", query)
        total = int(first.get("total_results") or 0)
        if total > max_results:
            raise TooManyResults(total, max_results, label)
        results = list(first.get("results") or [])
        pages = max(1, math.ceil(total / PER_PAGE))
        if progress:
            progress(len(results), total)
        for page in range(2, pages + 1):
            if cancelled and cancelled():
                raise INatError("Cancelled.")
            query["page"] = str(page)
            data = self._get("/v2/observations", query)
            batch = data.get("results") or []
            if not batch:
                break
            results.extend(batch)
            if progress:
                progress(len(results), total)
        seen, ordered = set(), []
        for r in results:  # pagination can repeat records if data shifts mid-walk
            rid = r.get("id")
            if rid in seen or rid is None:
                continue
            seen.add(rid)
            ordered.append(r)
        return ordered, total

    def fetch_by_ids(self, ids: Iterable[int], progress: ProgressFn | None = None) -> list[dict]:
        """Observations by id, batched; ids that no longer exist are absent."""
        ids = sorted({int(i) for i in ids})
        out: list[dict] = []
        for start in range(0, len(ids), ID_BATCH):
            chunk = ids[start:start + ID_BATCH]
            query = {
                "id": ",".join(str(i) for i in chunk),
                "per_page": str(ID_BATCH),
                "locale": "en",
                "fields": OBSERVATION_FIELDS,
            }
            data = self._get("/v2/observations", query)
            out.extend(data.get("results") or [])
            if progress:
                progress(min(start + ID_BATCH, len(ids)), len(ids))
        return out

    def fetch_taxa(self, ids: Iterable[int]) -> dict[int, dict]:
        wanted = {int(i) for i in ids if i}
        found, missing = self.taxa_cache.get_many(wanted)
        fetched: dict[int, dict] = {}
        for start in range(0, len(missing), ID_BATCH):
            chunk = missing[start:start + ID_BATCH]
            data = self._get("/v2/taxa", {
                "id": ",".join(str(i) for i in chunk),
                "per_page": str(ID_BATCH),
                "fields": "(id:!t,name:!t,rank:!t)",
            })
            for t in data.get("results") or []:
                try:
                    fetched[int(t["id"])] = {"name": t.get("name") or "", "rank": t.get("rank") or ""}
                except (KeyError, TypeError, ValueError):
                    continue
        if fetched:
            self.taxa_cache.put_many(fetched)
            self.taxa_cache.save()
        found.update(fetched)
        return found

    def normalize_all(self, raws: list[dict]) -> dict[int, dict]:
        """Normalize raw observations, resolving ancestor names in batched lookups."""
        anc: set[int] = set()
        for r in raws:
            t = r.get("taxon") or {}
            for a in t.get("ancestor_ids") or []:
                if isinstance(a, int):
                    anc.add(a)
        taxa = self.fetch_taxa(anc) if anc else {}
        out: dict[int, dict] = {}
        for r in raws:
            n = normalize_observation(r, taxa)
            if n:
                out[n["id"]] = n
        return out


__all__ = [
    "build_source", "source_params", "BudgetExceeded", "INatClient", "INatError", "SourceError", "TaxaCache", "TooManyResults",
    "format_taxon_name", "normalize_observation", "parse_source_input", "photo_url", "username_url",
]
