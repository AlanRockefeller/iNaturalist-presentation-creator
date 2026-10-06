from app.models import AnnotationOverrides
from app.project import add_observations
from app.sorting import group_of, insert_sorted, passes_filters, sort_ids
from tests.conftest import make_obs
from tests.helpers import norm, project, source

URL = "https://www.inaturalist.org/observations?taxon_id=47170&place_id=1"

OBS = norm(
    make_obs(1, 47702, faves=3, observed_on="2026-01-05", created_at="2026-01-06T00:00:00Z"),   # Psilocybe cyanescens / Strophariaceae
    make_obs(2, 47692, faves=17, observed_on="2025-11-14", created_at="2026-02-01T00:00:00Z"),  # Hygrocybe conica / Hygrophoraceae
    make_obs(3, 47602, faves=0, observed_on="2026-03-01", created_at="2025-12-01T00:00:00Z"),   # Amanita muscaria / Amanitaceae
    make_obs(4, 47693, faves=5, observed_on=None, created_at="2026-03-01T00:00:00Z"),           # Hygrocybe coccinea
    make_obs(5, 47701, faves=17, observed_on="2024-05-05", created_at="2026-04-01T00:00:00Z"),  # Psilocybe sp.
    make_obs(6, None, faves=1, observed_on="2026-02-02", created_at="2026-05-01T00:00:00Z"),    # no taxon
)


def proj(grouping="none", **kw):
    pr = project(source("a", "alice"), source("b", URL), grouping=grouping, **kw)
    return add_observations(pr, list(OBS), OBS, {i: ["a"] for i in OBS}, placement="append")


def test_taxonomic_family_genus_species():
    pr = proj()
    # Amanitaceae; Hygrophoraceae (H. coccinea, H. conica); Strophariaceae
    # (P. cyanescens, Psilocybe sp.); no taxon last
    assert sort_ids(pr.order, OBS, pr, "taxonomic") == [3, 4, 2, 1, 5, 6]
    assert sort_ids(pr.order, OBS, pr, "taxonomic", "desc") == [6, 5, 1, 2, 4, 3]


def test_taxonomic_uses_name_override():
    pr = proj()
    states = {s.id: s for s in pr.observations}
    states[4] = states[4].model_copy(update={"overrides": AnnotationOverrides(scientific="Hygrocybe zzz")})
    pr = pr.model_copy(update={"observations": list(states.values())})
    order = sort_ids(pr.order, OBS, pr, "taxonomic")
    assert order.index(2) < order.index(4)


def test_favorites_sort_descending_with_taxonomic_ties():
    pr = proj()
    assert sort_ids(pr.order, OBS, pr, "favorites", "desc") == [2, 5, 4, 1, 6, 3]
    assert sort_ids(pr.order, OBS, pr, "favorites", "asc") == [3, 6, 1, 4, 2, 5]


def test_date_sorts_and_missing_dates_last():
    pr = proj()
    assert sort_ids(pr.order, OBS, pr, "observed_on", "asc") == [5, 2, 1, 6, 3, 4]
    assert sort_ids(pr.order, OBS, pr, "created_at", "asc") == [3, 1, 2, 4, 5, 6]


def test_original_order_and_random():
    pr = proj()
    ws = {"source_results": {"a": [5, 3, 1], "b": [6, 2, 4, 3]}}
    assert sort_ids(pr.order, OBS, pr, "original", "asc", ws) == [5, 3, 1, 6, 2, 4]
    r1 = sort_ids(pr.order, OBS, pr, "random", seed=7)
    assert sorted(r1) == sorted(pr.order)
    assert r1 == sort_ids(pr.order, OBS, pr, "random", seed=7)


def test_grouping_by_family_puts_groups_first():
    pr = proj(grouping="family")
    order = sort_ids(pr.order, OBS, pr, "favorites", "desc")
    labels = [group_of(OBS[i], None, "family", pr)[1] for i in order]
    assert labels == ["Amanitaceae", "Hygrophoraceae", "Hygrophoraceae", "Strophariaceae", "Strophariaceae", "Unclassified"]
    assert order == [3, 2, 4, 5, 1, 6]  # favorites desc inside each family


def test_grouping_by_genus_and_source():
    pr = proj(grouping="genus")
    assert group_of(OBS[5], None, "genus", pr)[1] == "Psilocybe"
    pr = proj(grouping="source")
    st = {s.id: s for s in pr.observations}
    st6 = st[6].model_copy(update={"source_ids": ["b"]})
    assert group_of(OBS[6], st6, "source", pr)[1] == pr.sources[1].label
    assert group_of(OBS[1], st[1], "source", pr)[1] == "alice"


def test_min_favorites_filter():
    pr = proj(min_faves=5)
    st = {s.id: s for s in pr.observations}
    assert [i for i in pr.order if passes_filters(OBS[i], st[i], pr)] == [2, 4, 5]
    pr = proj(min_faves=0)
    st = {s.id: s for s in pr.observations}
    assert all(passes_filters(OBS[i], st[i], pr) for i in pr.order)


def test_unavailable_placeholders_sort_last():
    pr = proj()
    partial = {k: v for k, v in OBS.items() if k != 2}
    assert sort_ids(pr.order, partial, pr, "taxonomic")[-1] == 2


def test_resort_preserves_selections_and_overrides():
    pr = proj()
    states = {s.id: s for s in pr.observations}
    states[1] = states[1].model_copy(update={"selected_photo_ids": [], "overrides": AnnotationOverrides(scientific="X")})
    pr = pr.model_copy(update={"observations": list(states.values())})
    pr2 = pr.model_copy(update={"order": sort_ids(pr.order, OBS, pr, "favorites", "desc")})
    st2 = {s.id: s for s in pr2.observations}
    assert st2[1].selected_photo_ids == [] and st2[1].overrides.scientific == "X"
    assert pr2.settings == pr.settings


def test_insert_sorted_keeps_manual_order():
    base = {k: v for k, v in OBS.items() if k in (1, 2, 3, 6)}
    pr = project(source("a", "alice"))
    pr = add_observations(pr, [6, 1, 3, 2], base, {}, placement="append")  # hand-arranged
    assert pr.order == [6, 1, 3, 2]
    pr = pr.model_copy(update={"settings": pr.settings.model_copy(update={
        "sort": pr.settings.sort.model_copy(update={"key": "custom", "base_key": "taxonomic"})})})
    new = insert_sorted(pr.order, [4, 5], OBS, pr)
    # taxonomic: 3 Amanita, 4 H. coccinea, 2 H. conica, 1 P. cyanescens, 5 Psilocybe sp., 6
    # 4 follows 3 (its taxonomic predecessor), 5 follows 1; existing order untouched
    assert new == [6, 1, 5, 3, 4, 2]
    assert [i for i in new if i not in (4, 5)] == pr.order


def test_add_observations_append_vs_sorted():
    base = {k: v for k, v in OBS.items() if k in (1, 3)}
    pr = add_observations(project(source("a", "alice")), [3, 1], base, {}, placement="append")
    appended = add_observations(pr, [4], OBS, {}, placement="append")
    assert appended.order == [3, 1, 4]
    sorted_in = add_observations(pr, [4], OBS, {}, placement="sorted")
    assert sorted_in.order == [3, 4, 1]
