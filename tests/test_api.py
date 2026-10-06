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
    assert all(sorted(o["selected_photo_ids"]) == sorted(o["known_photo_ids"]) for o in project["observations"])
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
    assert saved["format"] == "dikarya-presentation" and saved["schema_version"] == 1
    text = json.dumps(saved)
    assert "square.jpg" not in text and "faves_count" not in text

    # time passes on iNaturalist
    obs1 = next(o for o in saved["observations"] if o["id"] == 1)
    obs1["overrides"]["scientific"] = "Hygrocybe custom"
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
    r = app_client.post("/api/project/validate", content=json.dumps({"format": "dikarya-presentation", "schema_version": 7}))
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
