"""End-to-end tests through the HTTP API with a fake iNaturalist."""

import copy
import io
import json

from pptx import Presentation

from tests.conftest import make_obs, wait_job

URL_A = "https://www.inaturalist.org/observations?taxon_id=47170&place_id=1"
URL_B = "https://www.inaturalist.org/observations?taxon_id=47170&place_id=2"


def _seed(fake):
    fake.add(
        make_obs(1, 47692, user="alice", faves=17, place_id=1, photos=((11, 1600, 1200), (12, 1200, 1600))),
        make_obs(2, 47602, user="bob", faves=0, place_id=1, photos=((21, 1000, 1000),)),
        make_obs(3, 47702, user="alice", faves=3, place_id=2, photos=((31, 2048, 1365),)),
        make_obs(4, 47691, user="carol", faves=1, place_id=2, photos=((41, 800, 600),)),
    )


def _source(tc, url, sid, username=""):
    r = tc.post("/api/sources/parse", json={"url": url, "username": username})
    assert r.status_code == 200, r.text
    src = r.json()
    src.update({"id": sid, "enabled": True})
    return src


def _new_project(tc, *sources, **settings):
    project = {"format": "dikarya-presentation", "schema_version": 1, "sources": list(sources)}
    if settings:
        project["settings"] = settings
    return project


def _load(tc, project, workspace_id=None, refresh=None):
    job = tc.post("/api/load", json={"project": project, "workspace_id": workspace_id, "refresh_source_ids": refresh}).json()
    done = wait_job(tc, job["id"])
    assert done["state"] == "done", done
    return done["result"]


def test_full_flow_two_overlapping_sources_to_pptx(app_client, fake):
    tc = app_client
    _seed(fake)
    project = _new_project(tc, _source(tc, URL_A, "a"), _source(tc, URL_B, "b"), title="Fungi Night", presenter="Alan")
    result = _load(tc, project)
    assert result["first_load"]
    assert result["summary"]["auto_added"] == 4
    project = result["project"]
    assert {o["id"] for o in project["observations"]} == {1, 2, 3, 4}
    # only the first photo of each observation starts selected
    assert all(o["selected_photo_ids"] == o["known_photo_ids"][:1] for o in project["observations"])
    next(o for o in project["observations"] if o["id"] == 1)["selected_photo_ids"] = [11, 12]
    wid = result["workspace_id"]

    # favorites sort, ungrouped
    project["settings"]["grouping"] = "none"
    order = tc.post("/api/sort", json={"project": project, "workspace_id": wid, "key": "favorites", "direction": "desc"}).json()["order"]
    assert order == [1, 3, 4, 2]
    project["order"] = order

    plan = tc.post("/api/plan", json={"project": project, "workspace_id": wid}).json()
    assert plan["counts"]["images"] == 5
    assert [s.get("photo_id") for s in plan["slides"][1:3]] == [11, 12]

    job = tc.post("/api/generate", json={"project": project, "workspace_id": wid}).json()
    done = wait_job(tc, job["id"])
    assert done["state"] == "done", done
    assert done["filename"] == "Fungi_Night.pptx"
    resp = tc.get(done["download_url"])
    assert resp.status_code == 200
    assert "Fungi_Night.pptx" in resp.headers["content-disposition"]
    prs = Presentation(io.BytesIO(resp.content))
    assert len(prs.slides) == 6
    assert prs.slide_width * 9 == prs.slide_height * 16
    assert all(p.endswith("original.jpg") for p in fake.photo_requests)
    assert len(fake.photo_requests) == 5

    # regenerate: originals come from the short-lived cache, not iNaturalist
    job = tc.post("/api/generate", json={"project": project, "workspace_id": wid}).json()
    assert wait_job(tc, job["id"])["state"] == "done"
    assert len(fake.photo_requests) == 5


def test_saved_project_refresh_finds_new_and_unavailable(app_client, fake):
    tc = app_client
    _seed(fake)
    project = _new_project(tc, _source(tc, "https://www.inaturalist.org/observations?taxon_id=47170", "a", username="alice"), _source(tc, URL_B, "b"))
    first = _load(tc, project)
    saved = tc.post("/api/project/export", json={"project": first["project"]}).json()
    assert saved["format"] == "dikarya-presentation" and saved["schema_version"] == 8
    text = json.dumps(saved)
    assert "square.jpg" not in text and "faves_count" not in text

    # time passes on iNaturalist
    obs1 = next(o for o in saved["observations"] if o["id"] == 1)
    # Default observation fields are left out of the file to keep it small.
    assert "overrides" not in obs1 and "rotations" not in obs1 and "status" not in obs1
    obs1["overrides"] = {"scientific": "Hygrocybe custom"}
    fake.remove(4)
    fake.add(make_obs(5, 47693, user="alice", place_id=2), make_obs(6, 47602, user="dave", place_id=2),
             make_obs(7, 47602, user="alice", place_id=1))
    loaded = tc.post("/api/project/validate", content=json.dumps(saved)).json()["project"]
    calls_before = len(fake.api_calls())
    refreshed = _load(tc, loaded)
    s = refreshed["summary"]
    assert not refreshed["first_load"]
    assert s["unavailable"] == 1
    assert sorted(s["new_ids"]) == [5, 6, 7]       # 5 is in both sources but counted once
    assert s["new_by_source"] == {"a": 2, "b": 2}  # 5,7 via alice; 5,6 via URL B
    states = {o["id"]: o for o in refreshed["project"]["observations"]}
    assert states[4]["status"] == "unavailable"
    assert states[1]["overrides"]["scientific"] == "Hygrocybe custom"
    assert 5 not in states  # not silently added
    # known observation 4 that vanished was looked up by id in one batched request
    id_lookups = [r for r in fake.api_calls("/v2/observations")[calls_before:] if "id" in r.url.params]
    assert len(id_lookups) == 1

    # add selected (5 with no photos? no: 5 with all, 6 ignored), insert by sort
    added = tc.post("/api/observations/add", json={
        "project": refreshed["project"], "workspace_id": refreshed["workspace_id"],
        "observation_ids": [5], "selected_photo_ids": {}, "placement": "sorted", "ignore_ids": [6],
    }).json()["project"]
    states = {o["id"]: o for o in added["observations"]}
    assert 5 in states and 5 in added["order"] and 6 not in states
    assert 6 in added["ignored_observation_ids"]
    again = _load(tc, added)
    assert sorted(again["summary"]["new_ids"]) == [7]  # ignored 6 is not offered again


def test_incremental_source_add_only_queries_new_source(app_client, fake):
    tc = app_client
    _seed(fake)
    project = _new_project(tc, _source(tc, URL_A, "a"))
    first = _load(tc, project)
    project = first["project"]
    project["sources"].append(_source(tc, URL_B, "b"))
    before = len(fake.api_calls("/v2/observations"))
    second = _load(tc, project, first["workspace_id"], ["b"])
    calls = fake.api_calls("/v2/observations")[before:]
    assert len(calls) == 1 and calls[0].url.params["place_id"] == "2"
    assert sorted(second["summary"]["new_ids"]) == [3, 4]


def test_too_many_observations_is_a_clear_error(app_client, fake, settings):
    settings.max_observations_per_source = 2
    _seed(fake)
    tc = app_client
    project = _new_project(tc, _source(tc, "https://www.inaturalist.org/observations?taxon_id=47170", "a", username="alice"), _source(tc, URL_B, "b"))
    project["sources"][0]["url"] = "https://www.inaturalist.org/observations?taxon_id=47170"
    project["sources"][0]["type"] = "url"
    job = tc.post("/api/load", json={"project": project}).json()
    done = wait_job(tc, job["id"])
    assert done["state"] == "error"
    assert "matches 4 observations" in done["error"] and "Add filters" in done["error"]


def test_parse_endpoint_rejects_bad_sources(app_client):
    r = app_client.post("/api/sources/parse", json={"url": "http://169.254.169.254/latest"})
    assert r.status_code == 422
    assert "iNaturalist" in r.json()["detail"]


def test_invalid_project_file(app_client):
    assert app_client.post("/api/project/validate", content=b"{not json").status_code == 422
    r = app_client.post("/api/project/validate", content=json.dumps({"format": "dikarya-presentation", "schema_version": 9}))
    assert r.status_code == 422 and "newer" in r.json()["detail"]


def test_body_size_limit(app_client, settings):
    big = b"{" + b" " * (settings.max_body_bytes + 10) + b"}"
    assert app_client.post("/api/project/validate", content=big).status_code == 413


def test_expired_workspace_and_unknown_job(app_client):
    project = {"format": "dikarya-presentation", "schema_version": 1, "sources": []}
    r = app_client.post("/api/plan", json={"project": project, "workspace_id": "x" * 30})
    assert r.status_code == 410
    r = app_client.post("/api/plan", json={"project": project, "workspace_id": "../../etc/passwd"})
    assert r.status_code in (410, 422)
    assert app_client.get("/api/jobs/nope").status_code == 404
    assert app_client.get("/api/jobs/nope/download").status_code == 404


def test_generate_refuses_empty_and_oversized(app_client, fake, settings):
    tc = app_client
    _seed(fake)
    result = _load(tc, _new_project(tc, _source(tc, URL_A, "a")))
    empty = copy.deepcopy(result["project"])
    for o in empty["observations"]:
        o["selected_photo_ids"] = []
    r = tc.post("/api/generate", json={"project": empty, "workspace_id": result["workspace_id"]})
    assert r.status_code == 429 and "No photos" in r.json()["detail"]
    settings.max_image_slides = 1
    project = result["project"]
    r = tc.post("/api/generate", json={"project": project, "workspace_id": result["workspace_id"]})
    assert r.status_code == 429 and "limit" in r.json()["detail"]


def test_security_headers_and_no_stack_traces(app_client):
    r = app_client.get("/")
    assert r.status_code == 200
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert "Dikarya" in r.text
    assert app_client.get("/docs").status_code == 404
    assert app_client.get("/openapi.json").status_code == 404
    r = app_client.post("/api/sort", json={"project": "nope"})
    assert r.status_code == 422 and "Traceback" not in r.text
    assert app_client.get("/static/../app/main.py").status_code == 404


def test_user_url_with_any_values_loads_only_matching(app_client, fake):
    """Regression: a filtered URL plus a username must not load the whole account."""
    _seed(fake)
    fake.add(*[make_obs(100 + i, 47692, user="alice", place_id=3) for i in range(30)])
    tc = app_client
    src = _source(tc, "https://www.inaturalist.org/observations?place_id=2&subview=map&user_id=alice&verifiable=any", "a", username="alice")
    assert src["type"] == "username"
    result = _load(tc, _new_project(tc, src))
    assert sorted(o["id"] for o in result["project"]["observations"]) == [3]
    sent = fake.api_calls("/v2/observations")[0].url.params
    assert sent["place_id"] == "2" and sent["user_id"] == "alice" and "verifiable" not in sent


def test_healthz_reports_active_job_counts(app_client, fake):
    data = app_client.get("/healthz").json()
    assert data["ok"] is True and data["jobs"] == {"loading": 0, "generating": 0, "notes": 0}


def test_add_skips_observations_only_a_removed_source_matched(app_client, fake):
    tc = app_client
    _seed(fake)
    project = _new_project(tc, _source(tc, URL_A, "a"))
    first = _load(tc, project)
    project = first["project"]
    project["sources"].append(_source(tc, URL_B, "b"))
    second = _load(tc, project, first["workspace_id"], ["b"])
    assert sorted(second["summary"]["new_ids"]) == [3, 4]
    project = second["project"]
    project["sources"] = [s for s in project["sources"] if s["id"] != "b"]  # removed before review
    added = tc.post("/api/observations/add", json={
        "project": project, "workspace_id": second["workspace_id"],
        "observation_ids": [3, 4], "selected_photo_ids": {}, "placement": "append", "ignore_ids": [],
    }).json()["project"]
    assert {o["id"] for o in added["observations"]} == {1, 2}


def test_resume_with_live_workspace_does_not_search_again(app_client, fake):
    tc = app_client
    _seed(fake)
    first = _load(tc, _new_project(tc, _source(tc, URL_A, "a")))
    before = len(fake.api_calls("/v2/observations"))
    resumed = _load(tc, first["project"], first["workspace_id"], [])
    assert len(fake.api_calls("/v2/observations")) == before
    assert not resumed["first_load"] and resumed["summary"]["new"] == 0
    assert {o["id"] for o in resumed["observations"]} == {1, 2}


# ---------------------------------------------------------------- more photos of a species
MINE_HERE = "https://www.inaturalist.org/observations?place_id=1"


def _start_related(tc, project, workspace_id, refresh, taxa):
    job = tc.post("/api/load", json={"project": project, "workspace_id": workspace_id,
                                     "refresh_source_ids": refresh, "related_taxa": taxa}).json()
    return wait_job(tc, job["id"])


def _related_load(tc, project, workspace_id, refresh, taxa):
    done = _start_related(tc, project, workspace_id, refresh, taxa)
    assert done["state"] == "done", done
    return done["result"]


def _related_source(sid, whose, taxa):
    return {"id": sid, "type": "related", "url": "", "related": {"whose": whose, "taxon_ids": taxa}}


def _searches(fake, since):
    return [r for r in fake.api_calls("/v2/observations")[since:] if "id" not in r.url.params]


def test_more_photos_of_one_species_from_a_card(app_client, fake):
    tc = app_client
    fake.add(
        make_obs(1, 47692, user="alice", faves=2, place_id=1, photos=((11, 900, 600),)),
        make_obs(2, 47602, user="alice", faves=0, place_id=1, photos=((21, 900, 600),)),
        make_obs(3, 47692, user="alice", faves=1, place_id=2, photos=((31, 900, 600),)),   # mine, elsewhere
        make_obs(4, 47692, user="bob", faves=3, place_id=3, photos=((41, 900, 600),)),     # CC BY
        make_obs(5, 47692, user="carol", faves=9, place_id=3, license="cc-by-nc"),          # not commercial
        make_obs(6, 47692, user="dave", faves=0, place_id=3, photos=((61, 900, 600),)),    # no favorites
        make_obs(7, 47602, user="erin", faves=4, place_id=3, photos=((71, 900, 600),)),
    )
    first = _load(tc, _new_project(tc, _source(tc, MINE_HERE, "a", "alice")))
    assert {o["id"] for o in first["project"]["observations"]} == {1, 2}
    project = first["project"]
    project["settings"]["related"] = {"username": "alice", "buttons": True}
    project["sources"].append(_related_source("o", "others", [47692]))

    # "More from others" on observation 1: one search, results offered for review.
    before = len(fake.api_calls("/v2/observations"))
    res = _related_load(tc, project, first["workspace_id"], ["o"], [47692])
    calls = _searches(fake, before)
    assert len(calls) == 1 and calls[0].url.params["taxon_id"] == "47692"
    assert calls[0].url.params["not_user_id"] == "alice" and calls[0].url.params["order_by"] == "votes"
    assert res["summary"]["new_ids"] == [4] and res["summary"]["related_found"] == [4]
    assert {o["id"] for o in res["project"]["observations"]} == {1, 2}  # not added until chosen
    assert res["project"]["sources"][1]["label"] == "Other people's photos"

    added = tc.post("/api/observations/add", json={
        "project": res["project"], "workspace_id": res["workspace_id"], "observation_ids": [4],
        "selected_photo_ids": {}, "placement": "after", "after_id": 1, "ignore_ids": [],
    }).json()["project"]
    assert added["order"] == [2, 1, 4]
    st4 = next(s for s in added["observations"] if s["id"] == 4)
    assert st4["source_ids"] == ["o"] and st4["selected_photo_ids"] == [41]

    # Another species on the same source: membership only grows.
    added["sources"][1]["related"]["taxon_ids"].append(47602)
    res2 = _related_load(tc, added, res["workspace_id"], ["o"], [47602])
    assert res2["summary"]["related_found"] == [7]
    assert next(s for s in res2["project"]["observations"] if s["id"] == 4)["source_ids"] == ["o"]
    assert res2["summary"]["no_longer_matching"] == 0

    # My own photos of that species from anywhere.
    project3 = res2["project"]
    project3["sources"].append(_related_source("m", "mine", [47692]))
    res3 = _related_load(tc, project3, res2["workspace_id"], ["m"], [47692])
    assert res3["summary"]["related_found"] == [1, 3] and res3["summary"]["new_ids"][-1] == 3

    # Opening the project again does not repeat these searches; their
    # observations are refreshed by id.
    before = len(fake.api_calls("/v2/observations"))
    reopened = _load(tc, res3["project"])
    calls = fake.api_calls("/v2/observations")[before:]
    assert all("taxon_id" not in c.url.params for c in calls)
    assert any("4" in c.url.params.get("id", "").split(",") for c in calls)
    assert reopened["summary"]["unavailable"] == 0 and reopened["summary"]["new"] == 0


def test_more_of_mine_needs_a_username(app_client, fake):
    tc = app_client
    fake.add(make_obs(1, 47692, user="alice", faves=2, place_id=1))
    first = _load(tc, _new_project(tc, _source(tc, URL_A, "a")))
    project = first["project"]
    project["sources"].append(_related_source("m", "mine", [47692]))
    done = _start_related(tc, project, first["workspace_id"], ["m"], [47692])
    assert done["state"] == "error" and "username" in done["error"]


def test_more_photos_refuses_a_search_bigger_than_whats_left(app_client, fake, services):
    tc = app_client
    fake.add(make_obs(1, 47692, user="alice", faves=2, place_id=1))
    first = _load(tc, _new_project(tc, _source(tc, URL_A, "a")))
    project = first["project"]
    taxa = list(range(1000, 1300))
    project["sources"].append(_related_source("o", "others", taxa))
    budget = services.client.budget
    budget.limit = budget.limit - budget.remaining() + 100  # about 100 requests left today
    before = len(fake.api_calls())
    done = _start_related(tc, project, first["workspace_id"], ["o"], taxa)
    assert done["state"] == "error" and "300 species" in done["error"]
    assert len(fake.api_calls()) == before  # refused before asking iNaturalist anything
