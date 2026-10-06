from app.inaturalist import build_source, normalize_observation, parse_source_input
from app.models import Project, Source
from tests.conftest import TAXA

TAXA_INFO = {i: {"name": v[0], "rank": v[1]} for i, v in TAXA.items()}


def norm(*raws) -> dict[int, dict]:
    return {r["id"]: normalize_observation(r, TAXA_INFO) for r in raws}


def source(sid: str, text: str, enabled=True, label=None) -> Source:
    """A bare login means "this user's own observations" (a username source)."""
    if "/" not in text:
        p = build_source(f"https://www.inaturalist.org/observations?user_id={text}", text)
    else:
        p = parse_source_input(text)
    return Source(id=sid, type=p["type"], input=text, username=p["username"], url=p["url"],
                  label=label or p["label"], enabled=enabled)


def project(*sources, **settings) -> Project:
    pr = Project(sources=list(sources))
    if settings:
        pr = pr.model_copy(update={"settings": pr.settings.model_copy(update=settings)})
    return pr
