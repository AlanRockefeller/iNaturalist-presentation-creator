"""Rare finds: ranking a search's species by worldwide counts, picking their
observations, counts on later loads, and the slides. iNaturalist is the fake
in conftest.py: worldwide counts are its observations plus ``world_extra``."""

import io
import json

import pytest
from pptx import Presentation

from app import rare
from app.inaturalist import INatError
from app.models import DEFAULT_LICENSES, Project, RareSource
from app.project import allowed_licenses, load_project, observer_default
from app.sorting import sort_ids
from tests.conftest import make_obs, wait_job
from tests.test_api import _load, _source

URL = "https://www.inaturalist.org/observations?taxon_id=47170&place_id=1"
PARAMS = {"taxon_id": "47170", "place_id": "1"}


def _seed(fake):
    fake.add(
        make_obs(1, 47692, faves=3, place_id=1, photos=((11, 1000, 800),)),                 # Hygrocybe conica
        make_obs(2, 47692, faves=9, quality="needs_id", place_id=1, photos=((21, 1000, 800),)),
        make_obs(3, 47693, faves=1, place_id=1, photos=((31, 1000, 800),), license=None),  # coccinea, all rights reserved
        make_obs(4, 47603, faves=0, place_id=1, photos=((41, 1000, 800),)),                 # A. muscaria var. guessowii
        make_obs(5, 47602, faves=5, place_id=1, photos=((51, 1000, 800),)),                 # A. muscaria: not a leaf
        make_obs(6, 47702, faves=2, place_id=1, photos=((61, 1000, 800),)),                 # Psilocybe cyanescens
        make_obs(7, 47691, place_id=1, photos=((71, 1000, 800),)),                          # Hygrocybe sp.: a genus
        make_obs(8, 47702, place_id=2, photos=((81, 1000, 800),)),                          # outside the search
        make_obs(9, 47695, place_id=1, photos=((91, 1000, 800),)),                          # Hygrocybe 'PNW05'
    )
    # Worldwide: conica 502, coccinea 4, guessowii 2, cyanescens 42, PNW05 1.
    fake.world_extra = {47692: 500, 47693: 3, 47603: 1, 47702: 40, 47602: 30, 47695: 0}
    fake.cached_counts = {47692: 0}  # stale: conica looks like the rarest until it is counted


# ---------------------------------------------------------------- the search

def test_scan_keeps_species_and_finer_rarest_in_the_search_first(client, fake):
    _seed(fake)
    species, taxa = rare.scan_species(client, PARAMS)
    assert [s["id"] for s in species][-1] == 47692  # 2 in the search: last
    assert {s["id"] for s in species} == {47692, 47693, 47603, 47702, 47695}  # no genus, no non-leaf species
    assert taxa == 5


def test_ranking_uses_live_worldwide_counts_not_the_stale_cache(client, fake):
    _seed(fake)
    species, _ = rare.scan_species(client, PARAMS)
    top = rare.rank_species(client, species, 3)
    assert [(s["id"], s["world"]) for s in top] == [(47695, 1), (47603, 2), (47693, 4)]
    described = rare.rank_species(client, species, 3, described_only=True)
    assert [(s["id"], s["world"]) for s in described] == [(47603, 2), (47693, 4), (47702, 42)]


def test_a_species_with_observed_varieties_is_counted_on_its_own(client, fake):
    _seed(fake)
    assert rare.live_counts(client, [47602]) == {47602: 32}  # obs 4 and 5, plus 30 elsewhere
    counts = [r for r in fake.api_calls("/v2/observations") if r.url.params.get("per_page") == "0"]
    assert counts and counts[-1].url.params["taxon_id"] == "47602"
    assert rare.live_counts(client, [47602], scope=PARAMS) == {47602: 2}


def test_picks_the_best_observations_within_the_search(client, fake):
    _seed(fake)
    species = [{"id": 47603, "local": 1}, {"id": 47693, "local": 1}, {"id": 47702, "local": 1},
               {"id": 47692, "local": 2}]
    raws = rare.pick_observations(client, PARAMS, species, per_taxon=2)
    assert [r["id"] for r in raws] == [4, 3, 6, 1, 2]  # cyanescens: not obs 8; conica: research grade first
    commercial = rare.pick_observations(client, PARAMS, species, per_taxon=1, commercial_only=True)
    assert [r["id"] for r in commercial] == [4, 6, 1]  # coccinea's only photo is all rights reserved


def test_search_stays_within_its_request_estimate(client, fake):
    _seed(fake)
    before = len(fake.api_calls())
    result = rare.search(client, PARAMS, RareSource(top=3, described_only=True))
    assert [r["id"] for r in result["raws"]] == [4, 3, 6]
    assert [s["id"] for s in result["species"]] == [47603, 47693, 47702]
    assert len(fake.api_calls()) - before <= rare.estimate_requests(3)


class _HugeSearch:
    """A client whose search has 25,000 species, all seen once."""

    def __init__(self):
        self.pages = []

    def species_counts(self, params, page=1, per_page=500):
        self.pages.append(page)
        rows = [{"count": 1, "taxon": {"id": page * 1000 + i, "name": f"S {page} {i}", "rank_level": 10,
                                       "observations_count": 5}} for i in range(per_page)]
        return rows, 25_000


def test_a_huge_search_is_read_only_up_to_the_paging_ceiling():
    huge = _HugeSearch()
    species, taxa = rare.scan_species(huge, PARAMS)
    assert huge.pages == list(range(1, rare.MAX_SCAN_PAGES + 1)) and len(species) == 10_000 and taxa == 25_000


def test_cancel_stops_the_search(client, fake):
    _seed(fake)
    with pytest.raises(INatError, match="Cancelled"):
        rare.search(client, PARAMS, RareSource(), cancelled=lambda: True)


# ---------------------------------------------------------------- project rules

def _rare_source(tc, url=URL, sid="r", **settings):
    src = _source(tc, url, sid)
    src.update({"type": "rare", "label": "", "rare": {"top": 3, "per_taxon": 1, "described_only": True, **settings}})
    return src


def test_rare_sources_in_the_project_file(app_client):
    src = _rare_source(app_client)
    pr = load_project({"format": "dikarya-presentation", "schema_version": 7, "sources": [src]})
    assert pr.schema_version == 8 and pr.sources[0].type == "rare" and pr.sources[0].rare.top == 3
    assert pr.sources[0].label.startswith("Rare finds: ")
    assert observer_default(pr)  # other people's photos may be in it: credits on
    commercial = pr.model_copy(update={"sources": [pr.sources[0].model_copy(update={
        "rare": RareSource(commercial_only=True)})]})
    assert allowed_licenses(commercial, ["r"]) == set(DEFAULT_LICENSES)
    assert allowed_licenses(pr, ["r"]) is None
    bad = dict(src, rare=None)
    with pytest.raises(ValueError):
        load_project({"format": "dikarya-presentation", "schema_version": 8, "sources": [bad]})


def test_rarity_sort_puts_counted_observations_first():
    obs = {1: {"id": 1, "rare": {"world": 40}}, 2: {"id": 2}, 3: {"id": 3, "rare": {"world": 2}}}
    assert sort_ids([1, 2, 3], obs, Project(), "rarity") == [3, 1, 2]


# ---------------------------------------------------------------- loads and slides

def _project(*sources):
    return {"format": "dikarya-presentation", "schema_version": 8, "sources": list(sources)}


def _plan(tc, result):
    return tc.post("/api/plan", json={"project": result["project"], "workspace_id": result["workspace_id"]}).json()


def test_first_load_adds_the_rarest_and_the_slides_say_so(app_client, fake):
    tc = app_client
    _seed(fake)
    result = _load(tc, _project(_rare_source(tc)))
    project = result["project"]
    assert result["first_load"] and project["order"] == [4, 3, 6]
    assert project["settings"]["sort"]["key"] == "rarity"
    obs = {o["id"]: o for o in result["observations"]}
    assert obs[4]["rare"]["world"] == 2 and obs[4]["rare"]["local"] == 1 and obs[4]["rare"]["source"] == "r"

    plan = _plan(tc, result)
    images = [s for s in plan["slides"] if s["kind"] == "image"]
    lines = [next(line["text"] for line in s["lines"] if line["field"] == "rarity") for s in images]
    assert lines == ["2 observations worldwide · #1 rarest", "4 observations worldwide · #2 rarest",
                     "42 observations worldwide · #3 rarest"]
    assert "2 observations on iNaturalist worldwide as of " in images[0]["notes"]
    assert "1 of them in this search." in images[0]["notes"]
    assert plan["slides"][-1]["kind"] == "about" and plan["counts"]["about"] == 1

    # per-observation: hidden, or other text
    for st in project["observations"]:
        if st["id"] == 4:
            st["show_lines"] = {"rarity": False}
        if st["id"] == 3:
            st["overrides"] = {"rarity": "Only 4 known anywhere"}
    texts = [[line["text"] for line in s["lines"]] for s in _plan(tc, result)["slides"] if s["kind"] == "image"]
    assert not any("worldwide" in t for t in texts[0]) and "Only 4 known anywhere" in texts[1]

    # The About slide can be turned off.
    project["settings"]["about_slide"] = False
    assert _plan(tc, result)["slides"][-1]["kind"] == "image"


def test_the_deck_has_the_about_slide_and_no_favorites(app_client, fake):
    tc = app_client
    _seed(fake)
    result = _load(tc, _project(_rare_source(tc)))
    job = tc.post("/api/generate", json={"project": result["project"], "workspace_id": result["workspace_id"]}).json()
    done = wait_job(tc, job["id"])
    assert done["state"] == "done", done
    prs = Presentation(io.BytesIO(tc.get(done["download_url"]).content))
    last = " ".join(sh.text_frame.text for sh in prs.slides[-1].shapes if sh.has_text_frame)
    assert "About these numbers" in last and "any quality grade" in last
    first_photo = " ".join(sh.text_frame.text for sh in prs.slides[1].shapes if sh.has_text_frame)
    assert "2 observations worldwide · #1 rarest" in first_photo
    assert "favorite" not in json.dumps([sl.notes_slide.notes_text_frame.text for sl in prs.slides]).lower()


def test_refresh_counts_again_but_does_not_search_again(app_client, fake):
    tc = app_client
    _seed(fake)
    first = _load(tc, _project(_rare_source(tc)))
    scans = lambda: [r for r in fake.api_calls("/v2/observations/species_counts") if r.url.params.get("order") == "asc"]
    n_scans = len(scans())

    fake.world_extra[47603] = 9  # more seen elsewhere since
    again = _load(tc, first["project"])  # a full refresh, no workspace
    assert len(scans()) == n_scans
    assert again["source_results"] == {"r": 3}  # still loaded: Load must not search it again
    assert {o["id"]: o for o in again["observations"]}[4]["rare"]["world"] == 10

    before = len(fake.api_calls())
    resumed = _load(tc, again["project"], again["workspace_id"], [])
    assert len(fake.api_calls()) == before  # resuming asks iNaturalist nothing
    assert {o["id"]: o for o in resumed["observations"]}[4]["rare"]["world"] == 10

    searched = _load(tc, again["project"], again["workspace_id"], ["r"])  # Search again
    assert len(scans()) == n_scans + 1 and searched["summary"]["new"] == 0


def test_a_rare_source_added_later_offers_its_finds_for_review(app_client, fake):
    tc = app_client
    _seed(fake)
    first = _load(tc, _project(_source(tc, "https://www.inaturalist.org/observations?taxon_id=47170&place_id=2", "p2")))
    assert [st["id"] for st in first["project"]["observations"]] == [8]
    project = first["project"]
    project["sources"].append(_rare_source(tc))
    second = _load(tc, project, first["workspace_id"], ["r"])
    assert second["summary"]["new_ids"] == [4, 3, 6] and second["summary"]["related_found"] == [4, 3, 6]


def test_a_search_with_nothing_to_rank_is_a_clear_error(app_client, fake):
    tc = app_client
    fake.add(make_obs(1, 47691, place_id=1))  # a genus only
    job = tc.post("/api/load", json={"project": _project(_rare_source(tc)), "workspace_id": None,
                                     "refresh_source_ids": None}).json()
    done = wait_job(tc, job["id"])
    assert done["state"] == "error" and "nothing to rank" in done["error"]


def test_tied_counts_share_a_place():
    from app.presentation import rarity_text

    assert rarity_text({"world": 1}, (1, True)) == "1 observation worldwide · tied for #1 rarest"
    assert rarity_text({"world": 12}, (3, False)) == "12 observations worldwide · #3 rarest"
    assert rarity_text({"world": 12}) == "12 observations worldwide" and rarity_text(None) == ""
