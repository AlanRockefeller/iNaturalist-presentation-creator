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


def test_add_observations_selects_first_photo_and_respects_choices():
    pr = base_project()
    obs = norm(make_obs(1, 47692, photos=((11, 10, 10), (12, 10, 10), (13, 10, 10))), make_obs(2, 47602, photos=((21, 5, 5),)))
    pr = add_observations(pr, [1, 2], obs, {1: ["a"], 2: ["a", "b"]}, selected={2: []}, placement="append")
    st = {s.id: s for s in pr.observations}
    assert st[1].selected_photo_ids == [11]  # only the first photo by default
    assert st[1].known_photo_ids == [11, 12, 13]
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
    pr = add_observations(pr, [1, 2, 3], obs, {1: ["a"], 2: ["a"], 3: ["b"]},
                          selected={1: [11, 12]}, placement="append")
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
    assert data["format"] == "dikarya-presentation" and data["schema_version"] == 6
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


def test_v1_file_migrates_and_single_user_url_source_becomes_username():
    pr = load_project({
        "format": "dikarya-presentation", "schema_version": 1,
        "sources": [{"id": "a", "type": "url",
                     "url": "https://www.inaturalist.org/observations?taxon_id=47170&user_id=alan_rockefeller"}],
        "observations": [{"id": 1, "selected_photo_ids": [5]}],
    })
    assert pr.schema_version == 6
    assert pr.sources[0].type == "username" and pr.sources[0].username == "alan_rockefeller"
    assert pr.observations[0].rotations == {} and pr.observations[0].show_lines == {}


def test_rotations_are_kept_only_for_known_photos():
    pr = load_project({
        "format": "dikarya-presentation", "schema_version": 2,
        "observations": [{"id": 1, "selected_photo_ids": [5], "known_photo_ids": [5, 6],
                          "rotations": {"5": 90, "6": 0, "7": 180}}],
    })
    assert pr.observations[0].rotations == {5: 90}
    assert json.loads(json.dumps(project_to_json(pr)))["observations"][0]["rotations"] == {"5": 90}
    with pytest.raises(ProjectError):
        load_project({"format": "dikarya-presentation", "schema_version": 2,
                      "observations": [{"id": 1, "rotations": {"5": 45}}]})


def test_photo_order_keeps_only_known_photos_once():
    pr = load_project({
        "format": "dikarya-presentation", "schema_version": 4,
        "observations": [{"id": 1, "known_photo_ids": [5, 6, 7], "photo_order": [7, 9, 7, 5]}],
    })
    assert pr.observations[0].photo_order == [7, 5]


def test_add_observations_refuses_to_pass_the_project_limit():
    pr = base_project()
    obs = norm(make_obs(1, 47692), make_obs(2, 47602), make_obs(3, 47702))
    pr = add_observations(pr, [1], obs, {1: ["a"]}, placement="append", max_observations=2)
    with pytest.raises(ProjectError) as exc:
        add_observations(pr, [1, 2, 3], obs, {2: ["a"], 3: ["a"]}, placement="append", max_observations=2)
    assert "1 more can be added, not 2" in str(exc.value)
    assert len(add_observations(pr, [2], obs, {2: ["a"]}, placement="append", max_observations=2).observations) == 2


# ---------------------------------------------------------------- schema 5 and limits

USER_URL = "https://www.inaturalist.org/observations?taxon_id=47170&user_id=alan_rockefeller"


def test_v4_file_keeps_its_single_user_url_source_as_the_users_own():
    pr = load_project({"format": "dikarya-presentation", "schema_version": 4,
                       "sources": [{"id": "a", "type": "url", "url": USER_URL}]})
    assert pr.schema_version == 6
    assert pr.sources[0].type == "username" and pr.sources[0].username == "alan_rockefeller"


def test_v5_source_type_is_the_users_choice():
    """A search limited to one user can be marked as not the presenter's photos."""
    pr = load_project({"format": "dikarya-presentation", "schema_version": 5,
                       "sources": [{"id": "a", "type": "url", "url": USER_URL}]})
    assert pr.sources[0].type == "url" and pr.sources[0].username is None
    assert observer_default(pr) is True
    again = load_project(json.loads(json.dumps(project_to_json(pr))))
    assert again.sources[0].type == "url"
    mine = load_project({"format": "dikarya-presentation", "schema_version": 5,
                         "sources": [{"id": "a", "type": "username", "username": "alan_rockefeller", "url": USER_URL}]})
    assert mine.sources[0].type == "username" and observer_default(mine) is False


def test_ignored_ids_are_capped_so_the_file_still_opens():
    from app.models import MAX_IGNORED, Project

    pr = ignore_observations(Project(ignored_observation_ids=list(range(1, MAX_IGNORED - 100))), list(range(10**6, 10**6 + 500)))
    assert len(pr.ignored_observation_ids) == MAX_IGNORED
    assert pr.ignored_observation_ids[-1] == 10**6 + 499  # the newest are kept
    load_project(project_to_json(pr))


def test_observation_with_more_photos_than_a_file_holds():
    from app.models import MAX_PHOTOS

    many = tuple((900_000 + k, 100, 100) for k in range(MAX_PHOTOS + 20))
    obs = norm(make_obs(5, 47692, photos=many))
    assert len(obs[5]["photos"]) == MAX_PHOTOS
    pr = add_observations(base_project(), [5], obs, {5: ["a"]}, placement="append")
    # A refresh where a selected photo was deleted and the photo list is full.
    st = pr.observations[0].model_copy(update={"selected_photo_ids": [123], "known_photo_ids": [123]})
    pr = pr.model_copy(update={"observations": [st]})
    refreshed, summary = refresh_project(pr, obs, {"a": [5]})
    known = refreshed.observations[0].known_photo_ids
    assert len(known) == MAX_PHOTOS and 123 in known and summary["missing_photos"] == {"5": [123]}
    load_project(project_to_json(refreshed))


def test_saved_file_leaves_out_default_observation_fields():
    pr = base_project()
    obs = norm(make_obs(1, 47692), make_obs(2, 47602))
    pr = add_observations(pr, [1, 2], obs, {1: ["a"], 2: ["a"]}, placement="append")
    st = pr.observations[1].model_copy(update={
        "overrides": AnnotationOverrides(location=""), "rotations": {obs[2]["photos"][0]["id"]: 90}})
    pr = pr.model_copy(update={"observations": [pr.observations[0], st]})
    data = project_to_json(pr)
    first, second = data["observations"]
    assert set(first) == {"id", "selected_photo_ids", "known_photo_ids", "source_ids", "last_inat_name", "added_at"}
    assert second["overrides"] == {"location": ""} and "status" not in second
    assert data["settings"]["annotations"]["observer"] is None  # settings stay complete
    again = load_project(json.loads(json.dumps(data)))
    assert again.observations == pr.observations


def test_a_selected_photo_beyond_a_full_known_list_still_saves_and_opens():
    from app.models import MAX_PHOTOS

    data = {"format": "dikarya-presentation", "schema_version": 5,
            "observations": [{"id": 1, "known_photo_ids": list(range(1, MAX_PHOTOS + 1)), "selected_photo_ids": [9999]}]}
    pr = load_project(data)
    known = pr.observations[0].known_photo_ids
    assert len(known) == MAX_PHOTOS and 9999 in known
    again = load_project(json.loads(json.dumps(project_to_json(pr))))
    assert again.observations[0].selected_photo_ids == [9999]


# ---------------------------------------------------------------- more photos (schema 6)
def _related(sid, whose, taxa, **extra):
    return {"id": sid, "type": "related", "url": "", "related": {"whose": whose, "taxon_ids": taxa}, **extra}


def _with_related_settings(pr, **update):
    return pr.model_copy(update={"settings": pr.settings.model_copy(
        update={"related": pr.settings.related.model_copy(update=update)})})


def test_v5_file_loads_as_v6_with_default_more_photos_settings():
    pr = load_project({"format": "dikarya-presentation", "schema_version": 5,
                       "sources": [{"id": "a", "type": "url", "url": URL_CR}]})
    assert pr.schema_version == 6
    rel = pr.settings.related
    assert rel.username == "" and rel.buttons is False
    assert (rel.mine.per_taxon, rel.mine.min_faves) == (20, 0)
    assert rel.others.licenses == ["cc0", "cc-by", "cc-by-sa", "cc-by-nd"]
    assert (rel.others.per_taxon, rel.others.min_faves, rel.others.research_only) == (5, 1, True)


def test_related_sources_are_rebuilt_from_their_species_and_settings():
    pr = load_project({"format": "dikarya-presentation", "schema_version": 6,
                       "settings": {"related": {"username": "@Alan_Rockefeller"}},
                       "sources": [{"id": "a", "type": "url", "url": URL_CR},
                                   _related("m", "mine", [47692, 47692, 47602], username="someone", url="https://evil.example/"),
                                   _related("o", "others", [47692], label="Best of the rest")]})
    mine, others = pr.sources[1], pr.sources[2]
    assert pr.settings.related.username == "Alan_Rockefeller"
    assert mine.related.taxon_ids == [47692, 47602] and mine.username is None and mine.label == "More of my photos"
    assert mine.url.startswith("https://www.inaturalist.org/observations?") and "taxon_id=47692,47602" in mine.url
    assert "user_id=Alan_Rockefeller" in mine.url
    assert others.label == "Best of the rest" and "not_user_id=Alan_Rockefeller" in others.url
    assert "photo_license=cc0,cc-by,cc-by-sa,cc-by-nd" in others.url
    again = load_project(json.loads(json.dumps(project_to_json(pr))))
    assert again.sources == pr.sources
    # A "more from others" source may hold other people's photos: credits start on.
    own_only = project(source("a", USER))
    assert observer_default(own_only) is False
    assert observer_default(own_only.model_copy(update={"sources": own_only.sources + [others]})) is True


@pytest.mark.parametrize("data, msg", [
    ({"sources": [{"id": "m", "type": "related", "url": ""}]}, "no species list"),
    ({"sources": [_related("m", "sometimes", [1])]}, "invalid"),
    ({"sources": [_related("m", "mine", [0])]}, "invalid"),
    ({"settings": {"related": {"username": "not a login"}}}, "invalid"),
    ({"settings": {"related": {"others": {"licenses": []}}}}, "invalid"),
    ({"settings": {"related": {"others": {"licenses": ["cc-by-xyz"]}}}}, "invalid"),
])
def test_bad_more_photos_settings_are_rejected(data, msg):
    with pytest.raises(ProjectError, match=msg):
        load_project({"format": "dikarya-presentation", "schema_version": 6, **data})


def test_own_logins_come_from_mine_sources_and_more_photos():
    from app.project import is_own, own_logins
    pr = project(source("a", USER), source("b", URL_CR))
    assert own_logins(pr) == {USER}
    pr = _with_related_settings(pr, username="Second_Login")
    assert own_logins(pr) == {USER, "second_login"}
    assert is_own({"user": {"login": "SECOND_LOGIN"}}, pr) and not is_own({"user": {"login": "bob"}}, pr)


def test_added_observation_from_others_starts_with_its_first_allowed_photo():
    from app.models import Source
    from app.project import allowed_licenses
    obs = norm(make_obs(1, 47692, user="bob", licenses=["cc-by-nc", "cc-by"], photos=((11, 9, 9), (12, 9, 9))),
               make_obs(2, 47692, user="carol", licenses=["cc-by-nc"]))
    others = Source(id="o", type="related", url="", related={"whose": "others", "taxon_ids": [47692]})
    pr = project(source("a", URL_CR), others)
    assert allowed_licenses(pr, ["o"]) == {"cc0", "cc-by", "cc-by-sa", "cc-by-nd"}
    assert allowed_licenses(pr, ["o", "a"]) is None  # another source found it too: any photo
    pr = add_observations(pr, [1, 2], obs, {1: ["o"], 2: ["o", "a"]})
    st = {s.id: s for s in pr.observations}
    assert st[1].selected_photo_ids == [12] and st[2].selected_photo_ids == [1]


def test_placing_new_observations_right_after_one():
    obs = norm(*[make_obs(i, 47692) for i in range(1, 6)])
    member = {i: ["a"] for i in range(1, 6)}
    pr = add_observations(project(source("a", URL_CR)), [1, 2, 3], obs, member, placement="append")
    assert pr.settings.sort.key == "taxonomic"
    pr = add_observations(pr, [5, 4], obs, member, placement="after", after_id=2)
    assert pr.order == [1, 2, 5, 4, 3]
    assert pr.settings.sort.key == "custom" and pr.settings.sort.base_key == "taxonomic"
