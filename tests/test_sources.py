from urllib.parse import parse_qs, urlsplit

import pytest

from app.inaturalist import SourceError, build_source, parse_source_input, photo_url, source_params, username_url
from app.models import Source


@pytest.mark.parametrize("raw", ["alan_rockefeller", "@alan_rockefeller", "  alan_rockefeller  "])
def test_username_source(raw):
    src = parse_source_input(raw)
    assert src["type"] == "username"
    assert src["username"] == "alan_rockefeller"
    assert src["url"] == "https://www.inaturalist.org/observations?user_id=alan_rockefeller"
    assert src["params"] == {"user_id": "alan_rockefeller"}
    assert src["url"] == username_url("alan_rockefeller")


@pytest.mark.parametrize("raw", [
    "https://www.inaturalist.org/observations/alan_rockefeller",
    "https://www.inaturalist.org/people/alan_rockefeller",
    "inaturalist.org/people/alan_rockefeller",
])
def test_profile_urls_are_username_sources(raw):
    src = parse_source_input(raw)
    assert src["type"] == "username"
    assert src["username"] == "alan_rockefeller"


def test_observation_search_url_keeps_filters():
    src = parse_source_input(
        "https://www.inaturalist.org/observations?place_id=6793&taxon_id=47170&quality_grade=research"
        "&d1=2020-01-01&project_id=mushroom-club&subview=grid&page=4&per_page=12&iconic_taxa[]=Fungi"
    )
    assert src["type"] == "url"
    p = src["params"]
    assert p == {
        "place_id": "6793", "taxon_id": "47170", "quality_grade": "research", "d1": "2020-01-01",
        "project_id": "mushroom-club", "iconic_taxa": "Fungi",
    }
    # canonical URL is on www.inaturalist.org and round-trips to the same params
    parts = urlsplit(src["url"])
    assert parts.netloc == "www.inaturalist.org" and parts.path == "/observations"
    assert {k: v[0] for k, v in parse_qs(parts.query).items()} == p
    assert parse_source_input(src["url"])["params"] == p


def test_network_site_and_user_filter_url():
    src = parse_source_input("https://www.naturalista.mx/observations?user_id=someone&taxon_id=47170")
    assert src["type"] == "url"
    assert src["url"].startswith("https://www.inaturalist.org/observations?")
    assert src["params"]["user_id"] == "someone"


def test_project_and_single_observation_urls():
    assert parse_source_input("https://www.inaturalist.org/projects/fungi-of-x")["params"] == {"project_id": "fungi-of-x"}
    assert parse_source_input("https://www.inaturalist.org/observations/12345")["params"] == {"id": "12345"}


def test_unstable_ordering_dropped():
    p = parse_source_input("https://www.inaturalist.org/observations?taxon_id=1&order_by=random&order=sideways")["params"]
    assert "order_by" not in p and "order" not in p
    p = parse_source_input("https://www.inaturalist.org/observations?taxon_id=1&order_by=votes&order=desc")["params"]
    assert p["order_by"] == "votes" and p["order"] == "desc"


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "https://evil.example.com/observations?taxon_id=1",
    "https://inaturalist.org.evil.com/observations?taxon_id=1",
    "https://user:pw@www.inaturalist.org/observations?taxon_id=1",
    "https://www.inaturalist.org:8443/observations?taxon_id=1",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "ftp://www.inaturalist.org/observations?taxon_id=1",
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8000/healthz",
    "https://www.inaturalist.org/observations",  # no filters = all of iNaturalist
    "https://www.inaturalist.org/taxa/47170-Fungi",
    "https://www.inaturalist.org/observations/../../admin",
    "https://www.inaturalist.org/observations?taxon_id=1&bad%20key=1",
    "https://www.inaturalist.org/observations?taxon_id=" + "1" * 600,
    "https://www.inaturalist.org/observations?taxon_id=1\nx",
    "x" * 3000,
    "not a url at all",
    "../../etc/passwd",
])
def test_malicious_or_invalid_inputs_rejected(raw):
    with pytest.raises(SourceError):
        parse_source_input(raw)


def test_photo_url_allowlist():
    good = "https://inaturalist-open-data.s3.amazonaws.com/photos/123/square.jpg"
    assert photo_url(good, "original") == "https://inaturalist-open-data.s3.amazonaws.com/photos/123/original.jpg"
    assert photo_url("https://static.inaturalist.org/photos/9/square.JPG", "large").endswith("/photos/9/large.JPG")
    for bad in [
        "http://inaturalist-open-data.s3.amazonaws.com/photos/123/square.jpg",
        "https://evil.com/photos/123/square.jpg",
        "https://inaturalist-open-data.s3.amazonaws.com.evil.com/photos/1/square.jpg",
        "https://inaturalist-open-data.s3.amazonaws.com/photos/123/../../x/square.jpg",
        "https://inaturalist-open-data.s3.amazonaws.com/other/123/square.jpg",
        "https://u@inaturalist-open-data.s3.amazonaws.com/photos/1/square.jpg",
        "https://inaturalist-open-data.s3.amazonaws.com:444/photos/1/square.jpg",
    ]:
        assert photo_url(bad, "original") is None
    assert photo_url(good, "huge") is None


USER_URL = "https://www.inaturalist.org/observations?d1=2025-10-11&place_id=10&subview=map&user_id=alan_rockefeller&verifiable=any"


def test_website_any_values_are_dropped():
    p = parse_source_input(USER_URL)["params"]
    assert p == {"d1": "2025-10-11", "place_id": "10", "user_id": "alan_rockefeller"}
    p = parse_source_input("https://www.inaturalist.org/observations?taxon_id=1&quality_grade=any&place_id=ANY")["params"]
    assert p == {"taxon_id": "1"}


def test_url_without_username_is_a_search_source():
    src = build_source("https://www.inaturalist.org/observations?taxon_id=47170&place_id=10")
    assert src["type"] == "url" and src["username"] is None


def test_url_with_username_keeps_every_filter():
    src = build_source("https://www.inaturalist.org/observations?taxon_id=47170&place_id=10", "alan_rockefeller")
    assert src["type"] == "username" and src["username"] == "alan_rockefeller"
    assert src["params"] == {"taxon_id": "47170", "place_id": "10", "user_id": "alan_rockefeller"}
    assert src["label"].startswith("alan_rockefeller: ")
    # the user's own URL already filtered by them: same result, still "mine"
    same = build_source(USER_URL, "@Alan_Rockefeller")
    assert same["type"] == "username" and same["params"]["place_id"] == "10" and same["params"]["d1"] == "2025-10-11"


def test_username_source_round_trips_through_saved_url():
    src = build_source(USER_URL, "alan_rockefeller")
    saved = Source(id="a", type="username", username=src["username"], url=src["url"], input=src["input"])
    assert source_params(saved) == {"d1": "2025-10-11", "place_id": "10", "user_id": "alan_rockefeller"}


@pytest.mark.parametrize("url,username,msg", [
    ("", "alan_rockefeller", "observations URL"),
    ("   ", None, "observations URL"),
    ("alan_rockefeller", None, "looks like a username"),
    ("@alan_rockefeller", "", "looks like a username"),
    ("https://www.inaturalist.org/observations?user_id=someone_else&taxon_id=1", "alan_rockefeller", "someone_else"),
    ("https://www.inaturalist.org/observations?taxon_id=1", "bad name!", "not a valid iNaturalist username"),
    ("https://evil.example.com/observations?taxon_id=1", "alan_rockefeller", "iNaturalist"),
])
def test_build_source_rejections(url, username, msg):
    with pytest.raises(SourceError) as exc:
        build_source(url, username)
    assert msg in str(exc.value)
