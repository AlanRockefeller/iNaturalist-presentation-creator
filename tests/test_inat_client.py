import httpx
import pytest

from app.images import MediaFetcher
from app.inaturalist import (
    INatError, OBSERVATION_FIELDS, TooManyResults, format_taxon_name, normalize_observation,
)
from app.ratelimit import BudgetExceeded, ByteBudget, DailyBudget, RateLimiter
from tests.conftest import PHOTO_HOST, make_obs


def test_pagination_uses_max_per_page_and_fields(client, fake):
    fake.add(*[make_obs(i, 47692, photos=((i * 10, 800, 600),)) for i in range(1, 451)])
    raws, total = client.search({"user_id": "alice"}, 10_000)
    assert total == 450 and len(raws) == 450
    calls = fake.api_calls("/v2/observations")
    assert len(calls) == 3  # 200 + 200 + 50
    for c in calls:
        assert c.url.params["per_page"] == "200"
        assert c.url.params["fields"] == OBSERVATION_FIELDS
        assert c.url.params["locale"] == "en"
        assert c.url.params["photos"] == "true"
        assert c.headers["user-agent"].startswith("DikaryaPresentations/")


def test_too_many_results_refused_after_one_request(client, fake):
    fake.add(*[make_obs(i, 47692) for i in range(1, 30)])
    with pytest.raises(TooManyResults) as exc:
        client.search({"user_id": "alice"}, 10)
    assert "29" in str(exc.value)
    assert len(fake.api_calls()) == 1


def test_fetch_by_ids_batches_of_200(client, fake):
    fake.add(*[make_obs(i, 47692) for i in range(1, 451)])
    got = client.fetch_by_ids(list(range(1, 451)) + [99999])
    assert len(got) == 450
    calls = fake.api_calls("/v2/observations")
    assert len(calls) == 3
    assert all(len(c.url.params["id"].split(",")) <= 200 for c in calls)


def test_taxa_lookups_batched_and_cached(client, fake):
    fake.add(make_obs(1, 47692), make_obs(2, 47602), make_obs(3, 47702))
    raws, _ = client.search({"user_id": "alice"}, 100)
    first = client.normalize_all(raws)
    taxa_calls = fake.api_calls("/v2/taxa")
    assert len(taxa_calls) == 1
    assert first[1]["ranks"] == {
        "kingdom": "Fungi", "phylum": "Basidiomycota", "class": "Agaricomycetes",
        "order": "Agaricales", "family": "Hygrophoraceae", "genus": "Hygrocybe",
    }
    client.normalize_all(raws)
    assert len(fake.api_calls("/v2/taxa")) == 1  # second time served from cache


def test_retries_429(client, fake):
    fake.add(make_obs(1, 47692))
    fake.fail_429_times = 2
    raws, total = client.search({"user_id": "alice"}, 100)
    assert total == 1
    assert len(fake.api_calls()) == 3


def test_persistent_429_gives_friendly_error(client, fake):
    fake.fail_429_times = 99
    with pytest.raises(INatError):
        client.search({"user_id": "alice"}, 100)


def test_rate_limiter_spaces_requests():
    t = [0.0]
    sleeps = []

    def clock():
        return t[0]

    def sleep(d):
        sleeps.append(d)
        t[0] += d

    rl = RateLimiter(1.0, clock=clock, sleep=sleep)
    for _ in range(4):
        rl.wait()
    assert sleeps == [1.0, 1.0, 1.0]
    rl.penalize(10)
    rl.wait()
    assert sleeps[-1] == pytest.approx(10.0)


def test_daily_budget(tmp_path):
    b = DailyBudget(3, tmp_path / "b.json", today=lambda: "2026-10-06")
    for _ in range(3):
        b.consume()
    with pytest.raises(BudgetExceeded):
        b.consume()
    b.flush()
    again = DailyBudget(3, tmp_path / "b.json", today=lambda: "2026-10-06")
    assert again.remaining() == 0
    tomorrow = DailyBudget(3, tmp_path / "b.json", today=lambda: "2026-10-07")
    assert tomorrow.remaining() == 3


def test_byte_budget_windows():
    now = [1_000_000.0]
    bb = ByteBudget(hourly=100, daily=150, clock=lambda: now[0])
    bb.add(90)
    bb.check(10)
    with pytest.raises(BudgetExceeded):
        bb.check(11)
    now[0] += 3700  # hour passes, day does not
    bb.check(60)
    with pytest.raises(BudgetExceeded):
        bb.check(61)


def test_budget_exhaustion_stops_client(settings, fake):
    import httpx

    from app.inaturalist import INatClient

    c = INatClient(settings, http=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                   limiter=RateLimiter(0), budget=DailyBudget(1))
    fake.add(*[make_obs(i, 47692) for i in range(1, 300)])
    with pytest.raises(BudgetExceeded):
        c.search({"user_id": "alice"}, 10_000)


@pytest.mark.parametrize("taxon,ranks,expected", [
    (None, {}, ""),
    ({"id": 48460, "name": "Life", "rank": "stateofmatter"}, {}, ""),
    ({"id": 1, "name": "Amanita muscaria", "rank": "species"}, {}, "Amanita muscaria"),
    ({"id": 1, "name": "Psilocybe", "rank": "genus"}, {}, "Psilocybe sp."),
    ({"id": 1, "name": "Hygrophoraceae", "rank": "family"}, {}, "Hygrophoraceae"),
    ({"id": 1, "name": "Amanita muscaria guessowii", "rank": "variety"}, {}, "Amanita muscaria var. guessowii"),
    ({"id": 1, "name": "Caesareae", "rank": "section"}, {"genus": "Amanita"}, "Amanita sect. Caesareae"),
    ({"id": 1, "name": "Amanita muscaria", "rank": "complex"}, {}, "Amanita muscaria complex"),
])
def test_format_taxon_name(taxon, ranks, expected):
    assert format_taxon_name(taxon, ranks) == expected


def test_normalize_life_has_blank_name_and_hidden_photos_dropped():
    raw = make_obs(5, 48460, photos=((1, 10, 10), (2, 10, 10)))
    raw["photos"][1]["hidden"] = True
    raw["photos"].append({"id": 3, "url": "https://evil.com/photos/3/square.jpg"})
    n = normalize_observation(raw, {})
    assert n["inat_name"] == "" and n["common_name"] is None
    assert [p["id"] for p in n["photos"]] == [1]


def test_normalize_provisional_name():
    raw = make_obs(6, 47701, ofvs=[{"name": "Provisional Species Name", "value": "Psilocybe alimapensis nom. prov."}])
    n = normalize_observation(raw, {})
    assert n["inat_name"] == "Psilocybe sp."
    assert n["provisional_name"] == "Psilocybe alimapensis nom. prov."


def test_rejected_filter_is_named(client, fake):
    with pytest.raises(INatError) as exc:
        client.search({"verifiable": "any"}, 100)
    assert '"verifiable" filter' in str(exc.value)


def test_spent_byte_budget_stops_photo_downloads(settings, fake):
    fake.add(make_obs(1, 47170, photos=((11, 800, 600), (12, 800, 600))))
    budget = ByteBudget(10, 10 ** 12)
    media = MediaFetcher(settings, budget, http=httpx.Client(transport=httpx.MockTransport(fake.handler)))
    base = f"https://{PHOTO_HOST}/photos/{{}}/square.jpg"
    media.fetch_original(11, base.format(11))  # larger than the allowance: it goes over once
    assert budget.remaining()[0] == 0
    with pytest.raises(BudgetExceeded):
        media.fetch_original(12, base.format(12))
    assert len(fake.photo_requests) == 1
