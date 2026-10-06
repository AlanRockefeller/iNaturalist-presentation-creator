import json

import pytest

from app.models import AnnotationOverrides, ObservationState, SortSettings
from app.project import (
    ProjectError, add_observations, ignore_observations, load_project, merge_memberships,
    observer_default, observer_enabled, project_to_json, refresh_project, remove_source, safe_filename,
)
from tests.conftest import make_obs
from tests.helpers import norm, project, source

USER = "alan_rockefeller"
URL_CR = "https://www.inaturalist.org/observations?place_id=6924&taxon_id=47170"
URL_MX = "https://www.inaturalist.org/observations?place_id=6793&taxon_id=47170"


def base_project():
    return project(source("a", USER), source("b", URL_CR, label="Costa Rica"))


# ---------------------------------------------------------------- merging

def test_merge_deduplicates_and_keeps_membership():
    pool, members = merge_memberships({"a": [1, 2, 3], "b": [3, 4, 1]}, ["a", "b"])
    assert pool == [1, 2, 3, 4]
    assert members == {1: ["a", "b"], 2: ["a"], 3: ["a", "b"], 4: ["b"]}


def test_first_refresh_reports_everything_new_without_adding():
    pr = base_project()
    obs = norm(make_obs(1, 47692), make_obs(2, 47602), make_obs(3, 47702))
    out, summary = refresh_project(pr, obs, {"a": [1, 2], "b": [2, 3]})
    assert out.observations == []
    assert summary["new_ids"] == [1, 2, 3]
    assert summary["new_by_source"] == {"a": 2, "b": 2}
    assert summary["membership"]["2"] == ["a", "b"]


def test_add_observations_selects_all_photos_and_respects_choices():
    pr = base_project()
    obs = norm(make_obs(1, 47692, photos=((11, 10, 10), (12, 10, 10), (13, 10, 10))), make_obs(2, 47602, photos=((21, 5, 5),)))
    pr = add_observations(pr, [1, 2], obs, {1: ["a"], 2: ["a", "b"]}, selected={2: []}, placement="append")
    st = {s.id: s for s in pr.observations}
    assert st[1].selected_photo_ids == [11, 12, 13]
    assert st[2].selected_photo_ids == []
    assert st[2].source_ids == ["a", "b"]
    assert pr.order == [1, 2]


def test_remove_or_disable_source_keeps_shared_observations():
    pr = base_project()
    obs = norm(make_obs(1, 47692), make_obs(2, 47602), make_obs(3, 47702))
    pr = add_observations(pr, [1, 2, 3], obs, {1: ["a"], 2: ["a", "b"], 3: ["b"]}, placement="append")
    removed = remove_source(pr, "b")
    assert [s.id for s in removed.observations] == [1, 2]
    assert {s.id: s.source_ids for s in removed.observations}[2] == ["a"]
    assert removed.order == [1, 2]

    from app.sorting import passes_filters
    disabled = pr.model_copy(update={"sources": [pr.sources[0], pr.sources[1].model_copy(update={"enabled": False})]})
    st = {s.id: s for s in disabled.observations}
    assert passes_filters(obs[2], st[2], disabled)      # still in active source a
    assert not passes_filters(obs[3], st[3], disabled)  # only in disabled source b
    assert len(disabled.observations) == 3              # nothing removed by disabling


# ---------------------------------------------------------------- refresh

def saved_project():
    pr = base_project()
    obs = norm(
        make_obs(1, 47692, photos=((11, 10, 10), (12, 10, 10))),
        make_obs(2, 47701, photos=((21, 10, 10),)),
        make_obs(3, 47602, photos=((31, 10, 10), (32, 10, 10))),
    )
    pr = add_observations(pr, [1, 2, 3], obs, {1: ["a"], 2: ["a"], 3: ["b"]}, placement="append")
    states = {s.id: s for s in pr.observations}
    states[2] = states[2].model_copy(update={"overrides": AnnotationOverrides(scientific="Psilocybe alimapensis nom. prov.")})
    states[3] = states[3].model_copy(update={"selected_photo_ids": [32]})
    return pr.model_copy(update={"observations": [states[i] for i in (1, 2, 3)], "order": [3, 1, 2]})


def test_refresh_detects_new_unavailable_and_preserves_user_choices():
    pr = saved_project()
    # Now on iNat: obs 3 deleted; obs 1 lost photo 12, gained 13; obs 2 re-identified;
    # new obs 4 (both sources), 5 (source b), 6 (source a)
    live = norm(
        make_obs(1, 47692, photos=((11, 10, 10), (13, 10, 10))),
        make_obs(2, 47702, photos=((21, 10, 10),)),
        make_obs(4, 47693), make_obs(5, 47602), make_obs(6, 47691),
    )
    out, s = refresh_project(pr, live, {"a": [6, 1, 2, 4], "b": [4, 5]})
    st = {x.id: x for x in out.observations}

    assert s["updated"] == 2
    assert s["unavailable"] == 1 and st[3].status == "unavailable"
    assert 3 in out.order  # placeholder kept, not silently removed
    assert st[3].selected_photo_ids == [32]
    assert s["new_ids"] == [6, 4, 5]
    assert s["new"] == 3
    assert s["new_by_source"] == {"a": 2, "b": 2}  # 4 counted for both; 3 unique
    # deleted photo stays selected and is flagged, no substitute is selected
    assert st[1].selected_photo_ids == [11, 12]
    assert s["missing_photos"] == {"1": [12]}
    assert s["new_photos"] == {"1": [13]}
    # user's name override survives a re-identification
    assert st[2].overrides.scientific == "Psilocybe alimapensis nom. prov."
    assert s["name_changed"] == 1
    assert out.order == pr.order  # refresh never reorders


def test_refresh_ignored_and_disabled_sources():
    pr = saved_project()
    pr = ignore_observations(pr, [5])
    pr = pr.model_copy(update={"sources": [pr.sources[0], pr.sources[1].model_copy(update={"enabled": False})]})
    live = norm(make_obs(1, 47692, photos=((11, 1, 1), (12, 1, 1))), make_obs(2, 47701), make_obs(3, 47602),
                make_obs(4, 47693), make_obs(5, 47602), make_obs(7, 47602))
    # disabled source b was not queried this time
    out, s = refresh_project(pr, live, {"a": [1, 2, 4]})
    assert s["new_ids"] == [4]
    st = {x.id: x for x in out.observations}
    assert st[3].source_ids == ["b"]  # membership in an unqueried source is kept
    assert s["no_longer_matching"] == 0


def test_unavailable_observation_comes_back():
    pr = saved_project()
    gone, _ = refresh_project(pr, norm(make_obs(1, 47692), make_obs(2, 47701)), {"a": [1, 2]})
    back, s = refresh_project(gone, norm(make_obs(1, 47692), make_obs(2, 47701), make_obs(3, 47602, photos=((31, 1, 1), (32, 1, 1)))), {"a": [1, 2], "b": [3]})
    assert s["restored"] == 1
    assert {x.id: x.status for x in back.observations}[3] == "active"


# ---------------------------------------------------------------- observer default

def test_observer_defaults():
    assert observer_default(project(source("a", USER), source("b", "someone_else"))) is False
    assert observer_default(project(source("a", USER), source("b", URL_MX))) is True
    assert observer_default(project(source("b", URL_MX))) is True
    pr = project(source("a", USER))
    assert observer_enabled(pr) is False
    pr = pr.model_copy(update={"settings": pr.settings.model_copy(update={
        "annotations": pr.settings.annotations.model_copy(update={"observer": True})})})
    assert observer_enabled(pr) is True
    mixed = project(source("a", USER), source("b", URL_MX))
    off = mixed.model_copy(update={"settings": mixed.settings.model_copy(update={
        "annotations": mixed.settings.annotations.model_copy(update={"observer": False})})})
    assert observer_enabled(mixed) is True and observer_enabled(off) is False


# ---------------------------------------------------------------- JSON files

def test_save_load_roundtrip_has_everything_and_no_photos():
    pr = saved_project()
    pr = pr.model_copy(update={"settings": pr.settings.model_copy(update={
        "title": "Fungi of Marin", "presenter": "Alan", "background": "white", "grouping": "genus",
        "dividers": True, "min_faves": 2, "sort": SortSettings(key="custom", base_key="favorites", base_direction="desc"),
    })})
    pr = ignore_observations(pr, [99])
    data = project_to_json(pr)
    text = json.dumps(data)
    assert data["format"] == "dikarya-presentation" and data["schema_version"] == 1
    assert "base64" not in text and "square.jpg" not in text and "faves_count" not in text
    loaded = load_project(json.loads(text))
    assert loaded.order == [3, 1, 2]
    assert [s.url for s in loaded.sources] == [s.url for s in pr.sources]
    assert loaded.sources[0].username == USER and loaded.sources[0].type == "username"
    assert loaded.sources[1].label == "Costa Rica"
    st = {s.id: s for s in loaded.observations}
    assert st[3].selected_photo_ids == [32]
    assert st[2].overrides.scientific == "Psilocybe alimapensis nom. prov."
    assert st[1].source_ids == ["a"]
    assert loaded.settings.title == "Fungi of Marin" and loaded.settings.background == "white"
    assert loaded.settings.sort.key == "custom" and loaded.settings.sort.base_key == "favorites"
    assert loaded.settings.dividers and loaded.settings.grouping == "genus" and loaded.settings.min_faves == 2
    assert loaded.ignored_observation_ids == [99]
    assert len(text) < 5000


@pytest.mark.parametrize("data,msg", [
    ([], "JSON object"),
    ({"format": "something-else", "schema_version": 1}, "not a Dikarya"),
    ({"format": "dikarya-presentation"}, "schema_version"),
    ({"format": "dikarya-presentation", "schema_version": "1"}, "schema_version"),
    ({"format": "dikarya-presentation", "schema_version": 99}, "newer version"),
    ({"format": "dikarya-presentation", "schema_version": 0}, "Unsupported"),
    ({"format": "dikarya-presentation", "schema_version": 1, "observations": [{"id": -4}]}, "invalid"),
    ({"format": "dikarya-presentation", "schema_version": 1, "settings": {"background": "pink"}}, "invalid"),
    ({"format": "dikarya-presentation", "schema_version": 1, "sources": [
        {"id": "x", "type": "url", "url": "https://evil.example/observations?taxon_id=1"}]}, "not valid"),
    ({"format": "dikarya-presentation", "schema_version": 1, "sources": [
        {"id": "../../etc", "type": "url", "url": URL_MX}]}, "invalid"),
    ({"format": "dikarya-presentation", "schema_version": 1, "sources": [
        {"id": "x", "type": "url", "url": URL_MX}, {"id": "x", "type": "url", "url": URL_CR}]}, "duplicate"),
    # A username source with a bad URL is refused, not widened to the whole account.
    ({"format": "dikarya-presentation", "schema_version": 1, "sources": [
        {"id": "x", "type": "username", "username": "alan_rockefeller", "url": "ignored"}]}, "not valid"),
    ({"format": "dikarya-presentation", "schema_version": 1, "sources": [
        {"id": "x", "type": "username", "username": "alan_rockefeller", "url": ""}]}, "not valid"),
    ({"format": "dikarya-presentation", "schema_version": 1, "observations": [{"id": i} for i in range(1, 10_002)]}, "invalid"),
])
def test_invalid_project_files_rejected(data, msg):
    with pytest.raises(ProjectError) as exc:
        load_project(data)
    assert msg in str(exc.value)


def test_load_normalizes_inconsistent_order_and_dupes():
    pr = load_project({
        "format": "dikarya-presentation", "schema_version": 1,
        "sources": [{"id": "a", "type": "username", "username": USER,
                     "url": "https://www.inaturalist.org/observations?user_id=alan_rockefeller", "input": USER}],
        "observations": [{"id": 1, "selected_photo_ids": [5], "source_ids": ["a", "zzz"]}, {"id": 2}, {"id": 1}],
        "order": [2, 2, 77],
        "settings": {"title_photo": {"observation_id": 42, "photo_id": 1}},
        "unknown_future_key": True,
    })
    assert pr.order == [2, 1]
    assert [s.id for s in pr.observations] == [1, 2]
    assert pr.observations[0].known_photo_ids == [5]
    assert pr.observations[0].source_ids == ["a"]
    assert pr.sources[0].url == "https://www.inaturalist.org/observations?user_id=alan_rockefeller"
    assert pr.settings.title_photo is None


@pytest.mark.parametrize("title,expected", [
    ("Fungi of Marin County", "Fungi_of_Marin_County.pptx"),
    ("../../etc/passwd", "etcpasswd.pptx"),
    ("", "presentation.pptx"),
    ("   ///   ", "presentation.pptx"),
    ("Hongos de México 🍄", "Hongos_de_Mxico.pptx"),
    ("a" * 300, "a" * 80 + ".pptx"),
])
def test_safe_filename(title, expected):
    assert safe_filename(title, ".pptx") == expected
