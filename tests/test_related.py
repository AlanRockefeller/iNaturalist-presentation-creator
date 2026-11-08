""""More photos of the same species": search parameters and the request plan."""

import pytest

from app.inaturalist import INatError, SourceError
from app.models import RelatedOthers, RelatedSettings, RelatedMine
from app.related import api_filters, estimate_requests, is_species, run_related_search
from tests.conftest import make_obs
from tests.helpers import norm

CONICA, COCCINEA, MUSCARIA, GUESSOWII = 47692, 47693, 47602, 47603


def rs(username="alice", mine=None, **others):
    return RelatedSettings(username=username, mine=RelatedMine(**(mine or {})), others=RelatedOthers(**others))


def searches(fake):
    return [r for r in fake.api_calls("/v2/observations") if "taxon_id" in r.url.params]


def test_filters_for_my_photos_and_other_peoples():
    assert api_filters("mine", rs()) == {"user_id": "alice", "order_by": "votes", "order": "desc"}
    assert api_filters("mine", rs(mine={"min_faves": 2}))["popular"] == "true"
    with pytest.raises(SourceError, match="username"):
        api_filters("mine", rs(username=""))
    others = api_filters("others", rs())
    assert others == {
        "not_user_id": "alice", "quality_grade": "research", "photo_license": "cc0,cc-by,cc-by-sa,cc-by-nd",
        "order_by": "votes", "order": "desc", "popular": "true",
    }
    # Without a username nobody is left out; "all rights reserved" cannot be
    # asked of the API, so then there is no license filter at all.
    loose = api_filters("others", rs(username="", licenses=["cc-by", "arr"], research_only=False, min_faves=0))
    assert loose == {"order_by": "votes", "order": "desc"}


def test_estimate_is_a_ceiling():
    assert [estimate_requests(n) for n in (0, 1, 2, 100, 101, 250)] == [0, 1, 3, 101, 102, 253]


def test_one_species_is_one_request_with_the_most_favorited_first(client, fake):
    fake.add(*[make_obs(i, CONICA, user=f"u{i}", faves=i) for i in range(1, 9)])
    raws = run_related_search(client, "others", [CONICA], rs(per_taxon=3))
    assert [r["id"] for r in raws] == [8, 7, 6]
    assert len(searches(fake)) == 1
    assert searches(fake)[0].url.params["order_by"] == "votes"


def test_minimum_favorites_research_grade_and_own_photos(client, fake):
    fake.add(
        make_obs(1, CONICA, user="alice", faves=50),           # the presenter's own
        make_obs(2, CONICA, user="bob", faves=4),
        make_obs(3, CONICA, user="carol", faves=1),             # below the minimum
        make_obs(4, CONICA, user="dave", faves=9, quality="needs_id"),
    )
    raws = run_related_search(client, "others", [CONICA], rs(per_taxon=5, min_faves=2))
    assert [r["id"] for r in raws] == [2]
    mine = run_related_search(client, "mine", [CONICA], rs())
    assert [r["id"] for r in mine] == [1]


def test_licenses_filtered_by_the_api_or_here(client, fake):
    fake.add(
        make_obs(1, CONICA, user="bob", faves=5, license="cc-by-nc"),
        make_obs(2, CONICA, user="carol", faves=4, license=None),       # all rights reserved
        make_obs(3, CONICA, user="dave", faves=3, license="cc-by-nd"),
        make_obs(4, CONICA, user="erin", faves=2, licenses=["cc-by-nc", "cc0"], photos=((41, 10, 10), (42, 10, 10))),
    )
    default = run_related_search(client, "others", [CONICA], rs())
    assert [r["id"] for r in default] == [3, 4]
    assert searches(fake)[-1].url.params["per_page"] == "5"  # just the N it keeps
    with_arr = run_related_search(client, "others", [CONICA], rs(licenses=["cc0", "arr"]))
    assert [r["id"] for r in with_arr] == [2, 4]
    assert "photo_license" not in searches(fake)[-1].url.params


def test_a_variety_counts_under_the_species_searched(client, fake):
    fake.add(make_obs(1, GUESSOWII, user="bob", faves=3), make_obs(2, MUSCARIA, user="carol", faves=2))
    raws = run_related_search(client, "others", [MUSCARIA], rs(per_taxon=1))
    assert [r["id"] for r in raws] == [1]
    both = run_related_search(client, "others", [MUSCARIA, GUESSOWII], rs(per_taxon=1))
    assert [r["id"] for r in both] == [2, 1]  # each under its most specific species


def test_a_small_batch_is_paged_through(client, fake):
    fake.add(*[make_obs(i, CONICA, user="bob", faves=1) for i in range(1, 251)])
    fake.add(make_obs(999, COCCINEA, user="bob", faves=1))
    raws = run_related_search(client, "others", [CONICA, COCCINEA], rs(per_taxon=5))
    calls = searches(fake)
    assert len(calls) == 2 and calls[1].url.params["page"] == "2"  # 251 results: two pages for two species
    assert [r["id"] for r in raws][-1] == 999 and len(raws) == 6


def test_paging_stops_once_favorites_fall_below_the_minimum(client, fake):
    fake.add(*[make_obs(i, CONICA, user="bob", faves=1) for i in range(1, 301)])
    fake.add(make_obs(999, COCCINEA, user="bob", faves=1))
    assert run_related_search(client, "others", [CONICA, COCCINEA], rs(min_faves=3)) == []
    assert len(searches(fake)) == 1


def test_a_big_batch_asks_each_species_that_is_still_short(client, fake):
    fake.add(*[make_obs(i, CONICA, user="bob", faves=5) for i in range(1, 402)])
    fake.add(make_obs(999, COCCINEA, user="carol", faves=0))
    raws = run_related_search(client, "others", [CONICA, COCCINEA], rs(per_taxon=2, min_faves=0))
    calls = searches(fake)
    # The batch's first page fills Hygrocybe conica; only H. coccinea needs its own request.
    assert [c.url.params["taxon_id"] for c in calls] == [f"{CONICA},{COCCINEA}", str(COCCINEA)]
    assert calls[1].url.params["page"] == "1" and calls[1].url.params["per_page"] == "2"
    assert len(calls) <= estimate_requests(2)
    assert [r["id"] for r in raws] == [1, 2, 999]


def test_never_pages_past_ten_thousand(client, fake):
    with pytest.raises(INatError, match="10,000"):
        client.search_page({"taxon_id": "1"}, 51, 200)
    assert not fake.api_calls()


def test_cancel_stops_between_requests(client, fake):
    fake.add(make_obs(1, CONICA, user="bob", faves=5))
    with pytest.raises(INatError, match="Cancelled"):
        run_related_search(client, "others", [CONICA], rs(), cancelled=lambda: True)
    assert not fake.api_calls()


def test_is_species():
    obs = norm(make_obs(1, CONICA), make_obs(2, 47691), make_obs(3, GUESSOWII), make_obs(4, None))
    assert [is_species(obs[i]) for i in (1, 2, 3, 4)] == [True, False, True, False]
