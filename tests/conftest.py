"""Shared fixtures: an in-process fake of the iNaturalist API and photo hosts."""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.images import MediaFetcher  # noqa: E402
from app.inaturalist import INatClient, TaxaCache  # noqa: E402
from app.ratelimit import ByteBudget, DailyBudget, RateLimiter  # noqa: E402

PHOTO_HOST = "inaturalist-open-data.s3.amazonaws.com"

# id: (name, rank, parent)
TAXA = {
    48460: ("Life", "stateofmatter", None),
    47170: ("Fungi", "kingdom", 48460),
    47169: ("Basidiomycota", "phylum", 47170),
    50814: ("Agaricomycetes", "class", 47169),
    47167: ("Agaricales", "order", 50814),
    47168: ("Hygrophoraceae", "family", 47167),
    47691: ("Hygrocybe", "genus", 47168),
    47692: ("Hygrocybe conica", "species", 47691),
    47693: ("Hygrocybe coccinea", "species", 47691),
    47600: ("Amanitaceae", "family", 47167),
    47601: ("Amanita", "genus", 47600),
    47602: ("Amanita muscaria", "species", 47601),
    47603: ("Amanita muscaria guessowii", "variety", 47602),
    906595: ("Caesareae", "section", 47601),
    47700: ("Strophariaceae", "family", 47167),
    47701: ("Psilocybe", "genus", 47700),
    47702: ("Psilocybe cyanescens", "species", 47701),
}
COMMON = {47692: "Witch's Hat", 47602: "Fly Agaric", 47702: "Wavy Cap"}


def ancestors(tid: int) -> list[int]:
    chain = []
    while tid is not None:
        chain.append(tid)
        tid = TAXA[tid][2]
    return list(reversed(chain))


def make_obs(oid, taxon_id, user="alice", faves=0, observed_on="2026-10-01", photos=((1, 1600, 1200),),
             created_at=None, place="Point Reyes, CA, US", name=None, ofvs=None, place_id=1):
    taxon = None
    if taxon_id is not None:
        tname, rank, _ = TAXA[taxon_id]
        taxon = {
            "id": taxon_id, "name": tname, "rank": rank, "rank_level": 10,
            "preferred_common_name": COMMON.get(taxon_id), "ancestor_ids": ancestors(taxon_id),
        }
    return {
        "id": oid, "uuid": f"u-{oid}", "faves_count": faves, "observed_on": observed_on,
        "created_at": created_at or f"2026-10-{(oid % 28) + 1:02d}T10:00:00-07:00",
        "place_guess": place, "quality_grade": "research", "obscured": False,
        "user": {"login": user, "name": name},
        "taxon": taxon,
        "photos": [
            {"id": pid, "url": f"https://{PHOTO_HOST}/photos/{pid}/square.jpg", "hidden": False,
             "license_code": "cc-by", "attribution": f"(c) {user}, some rights reserved (CC BY)",
             "original_dimensions": {"width": w, "height": h}}
            for pid, w, h in photos
        ],
        "ofvs": ofvs or [],
        "_place_id": place_id,
    }


class FakeINat:
    """Implements the parts of api.inaturalist.org v2 and the photo bucket that the app uses."""

    def __init__(self):
        self.observations: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.photo_requests: list[str] = []
        self.photo_dims: dict[int, tuple[int, int]] = {}
        self.fail_429_times = 0
        self.deleted_photos: set[int] = set()

    def add(self, *obs):
        for o in obs:
            self.observations.append(o)
            for p in o["photos"]:
                d = p["original_dimensions"]
                self.photo_dims[p["id"]] = (d["width"], d["height"])

    def remove(self, oid):
        self.observations = [o for o in self.observations if o["id"] != oid]

    def api_calls(self, path=None):
        return [r for r in self.requests if path is None or r.url.path == path]

    def _public(self, o):
        return {k: v for k, v in o.items() if not k.startswith("_")}

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "api.inaturalist.org":
            self.requests.append(request)
            if self.fail_429_times > 0:
                self.fail_429_times -= 1
                return httpx.Response(429, headers={"Retry-After": "0"})
            if request.url.path == "/v2/observations":
                return self._observations(request)
            if request.url.path == "/v2/taxa":
                return self._taxa(request)
            return httpx.Response(404)
        if host == PHOTO_HOST:
            self.photo_requests.append(request.url.path)
            pid = int(request.url.path.split("/")[2])
            if pid in self.deleted_photos or pid not in self.photo_dims:
                return httpx.Response(404)
            w, h = self.photo_dims[pid]
            buf = io.BytesIO()
            Image.new("RGB", (w, h), (30 + pid % 200, 90, 60)).save(buf, "JPEG", quality=80)
            return httpx.Response(200, content=buf.getvalue(), headers={"Content-Type": "image/jpeg"})
        return httpx.Response(599)

    def _observations(self, request):
        q = request.url.params
        for key, value in q.multi_items():
            if value == "any":  # the real v2 API refuses website-only "any" values
                return httpx.Response(422, json={"status": 422, "errors": [{"path": key, "message": "must be equal to one of the allowed values"}]})
        per_page = int(q.get("per_page", 30))
        if "id" in q:
            ids = {int(i) for i in q["id"].split(",")}
            res = [self._public(o) for o in self.observations if o["id"] in ids]
            return httpx.Response(200, json={"total_results": len(res), "page": 1, "per_page": per_page, "results": res})
        res = list(self.observations)
        if "user_id" in q:
            res = [o for o in res if o["user"]["login"] == q["user_id"]]
        if "taxon_id" in q:
            want = {int(t) for t in q["taxon_id"].split(",")}
            res = [o for o in res if o["taxon"] and want & set(o["taxon"]["ancestor_ids"])]
        if "place_id" in q:
            res = [o for o in res if str(o["_place_id"]) == q["place_id"]]
        page = int(q.get("page", 1))
        chunk = res[(page - 1) * per_page: page * per_page]
        return httpx.Response(200, json={
            "total_results": len(res), "page": page, "per_page": per_page,
            "results": [self._public(o) for o in chunk],
        })

    def _taxa(self, request):
        ids = [int(i) for i in request.url.params["id"].split(",")]
        res = [{"id": i, "name": TAXA[i][0], "rank": TAXA[i][1]} for i in ids if i in TAXA]
        return httpx.Response(200, json={"total_results": len(res), "results": res})


@pytest.fixture
def fake():
    return FakeINat()


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.work_dir = tmp_path / "work"
    s.api_min_interval = 0
    s.ensure_dirs()
    return s


@pytest.fixture
def client(settings, fake):
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return INatClient(settings, http=http, limiter=RateLimiter(0, sleep=lambda d: None), budget=DailyBudget(10_000),
                      taxa_cache=TaxaCache(settings.state_dir / "taxa.json"), sleep=lambda s: None)


@pytest.fixture
def media(settings, fake):
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return MediaFetcher(settings, ByteBudget(10 ** 12, 10 ** 12), http=http)


@pytest.fixture
def services(settings, client, media):
    from app.main import Services

    return Services(settings, client=client, media=media)


@pytest.fixture
def app_client(settings, services):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings, services)) as tc:
        yield tc


def wait_job(tc, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = tc.get(f"/api/jobs/{job_id}").json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")
