"""Project-file validation, source merging, refresh diffing and project edits.

These functions are pure: they take a Project plus normalized observation
metadata and return new values, so they are easy to test without the network.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from .inaturalist import SourceError, build_source
from .models import (
    PROJECT_FORMAT, SCHEMA_VERSION, ObservationState, Project, Source,
)


class ProjectError(ValueError):
    """Invalid project file. Message is safe to show to users."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Loading / validation / migration
# ---------------------------------------------------------------------------

def _migrate(data: dict) -> dict:
    """Upgrade older schema versions in place. Version 1 is the first release,
    so this is the hook future versions extend (v1 -> v2 -> ...)."""
    version = data.get("schema_version")
    if version == 1:
        # v2 added per-photo rotations; a v1 file simply has none.
        data["schema_version"] = version = 2
    if version == 2:
        # v3 added per-observation show_lines; a v2 file simply has none.
        data["schema_version"] = version = 3
    if version == 3:
        # v4 added per-observation photo_order; a v3 file simply has none.
        data["schema_version"] = version = 4
    if version == SCHEMA_VERSION:
        return data
    raise ProjectError(f"Unsupported project schema version {version!r}.")


def load_project(data: Any) -> Project:
    """Validate an untrusted project document and return a normalized Project."""
    if not isinstance(data, dict):
        raise ProjectError("A project file must be a JSON object.")
    if data.get("format") != PROJECT_FORMAT:
        raise ProjectError("This is not a Dikarya presentation project file.")
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ProjectError("The project file has no valid schema_version.")
    if version > SCHEMA_VERSION:
        raise ProjectError(
            f"This project was saved by a newer version of the app (schema {version}); "
            f"this site understands schema {SCHEMA_VERSION}."
        )
    if version < 1:
        raise ProjectError(f"Unsupported project schema version {version}.")
    data = _migrate(dict(data))
    try:
        project = Project.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ProjectError(f"The project file is invalid ({where or 'structure'}: {first.get('msg', 'bad value')}).") from None
    return normalize_project(project)


def normalize_project(project: Project) -> Project:
    """Re-validate sources against the URL parser and make ids/order consistent."""
    sources: list[Source] = []
    seen_ids: set[str] = set()
    for src in project.sources:
        if src.id in seen_ids:
            raise ProjectError("The project file contains duplicate source ids.")
        seen_ids.add(src.id)
        try:
            if src.type == "username" and not src.username:
                raise SourceError("a username source needs a username")
            # build_source also recognises a URL that filters to a single user.
            parsed = build_source(src.url, src.username if src.type == "username" else None)
        except SourceError as exc:
            raise ProjectError(f"Source {src.label or src.id!r} is not valid: {exc}") from None
        sources.append(src.model_copy(update={
            "type": parsed["type"],
            "url": parsed["url"],
            "username": parsed["username"],
            "label": src.label or parsed["label"],
        }))
    source_ids = {s.id for s in sources}

    states: list[ObservationState] = []
    seen_obs: set[int] = set()
    for st in project.observations:
        if st.id in seen_obs:
            continue
        seen_obs.add(st.id)
        known = list(dict.fromkeys(st.known_photo_ids))
        selected = list(dict.fromkeys(st.selected_photo_ids))
        for pid in selected:
            if pid not in known:
                known.append(pid)
        states.append(st.model_copy(update={
            "known_photo_ids": known,
            "selected_photo_ids": selected,
            "rotations": {pid: r for pid, r in st.rotations.items() if r and pid in known},
            "photo_order": [pid for pid in dict.fromkeys(st.photo_order) if pid in known],
            "source_ids": [s for s in dict.fromkeys(st.source_ids) if s in source_ids],
        }))
    order = [i for i in dict.fromkeys(project.order) if i in seen_obs]
    in_order = set(order)
    order += [s.id for s in states if s.id not in in_order]
    settings = project.settings
    if settings.title_photo and settings.title_photo.observation_id not in seen_obs:
        settings = settings.model_copy(update={"title_photo": None})
    return project.model_copy(update={
        "sources": sources,
        "observations": states,
        "order": order,
        "settings": settings,
        "ignored_observation_ids": list(dict.fromkeys(project.ignored_observation_ids)),
    })


def project_to_json(project: Project) -> dict:
    from . import __version__

    data = project.model_dump(mode="json")
    data["format"] = PROJECT_FORMAT
    data["schema_version"] = SCHEMA_VERSION
    data["saved_at"] = now_iso()
    data["app_version"] = __version__
    return data


SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9 _\-]+")


def safe_filename(title: str, suffix: str, default: str = "presentation") -> str:
    """ASCII, no separators, bounded length; never empty."""
    base = SAFE_FILENAME_RE.sub("", (title or "").strip())
    base = re.sub(r"\s+", " ", base).strip(" .-_")[:80].strip()
    base = base.replace(" ", "_") or default
    return base + suffix


# ---------------------------------------------------------------------------
# Observer default
# ---------------------------------------------------------------------------

def observer_default(project: Project) -> bool:
    """Username-only projects are probably the presenter's own photos: no credit
    line by default. Any URL source may contain other people's photos: credit on."""
    active = [s for s in project.sources if s.enabled] or project.sources
    return any(s.type == "url" for s in active)


def observer_enabled(project: Project) -> bool:
    explicit = project.settings.annotations.observer
    return observer_default(project) if explicit is None else bool(explicit)


# ---------------------------------------------------------------------------
# Merging sources
# ---------------------------------------------------------------------------

def merge_memberships(source_results: dict[str, list[int]], source_order: list[str]) -> tuple[list[int], dict[int, list[str]]]:
    """Combine per-source result lists into one de-duplicated pool.

    Returns (ids in first-seen order, id -> source ids that matched it).
    """
    pool: list[int] = []
    membership: dict[int, list[str]] = {}
    for sid in source_order:
        for oid in source_results.get(sid, []):
            if oid not in membership:
                membership[oid] = []
                pool.append(oid)
            if sid not in membership[oid]:
                membership[oid].append(sid)
    return pool, membership


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

def refresh_project(
    project: Project,
    observations: dict[int, dict],
    source_results: dict[str, list[int]],
) -> tuple[Project, dict]:
    """Apply freshly fetched iNaturalist data to a project.

    ``observations``   normalized metadata for every observation that currently
                       exists (source results plus re-fetched known ids).
    ``source_results`` source id -> ids that source returned, in result order.

    Never adds observations: new ones are *reported* for review. Never drops
    them: missing ones become unavailable placeholders. User overrides are left
    untouched.
    """
    order_ids = [s.id for s in project.sources]
    pool, membership = merge_memberships(source_results, order_ids)
    enabled = {s.id for s in project.sources if s.enabled}
    known = {s.id for s in project.observations}
    ignored = set(project.ignored_observation_ids)

    summary = {
        "updated": 0, "unavailable": 0, "newly_unavailable": 0, "restored": 0,
        "name_changed": 0, "missing_photos": {}, "new_photos": {},
        "no_longer_matching": 0,
    }
    states = []
    for st in project.observations:
        obs = observations.get(st.id)
        if obs is None:
            summary["unavailable"] += 1
            if st.status != "unavailable":
                summary["newly_unavailable"] += 1
            states.append(st.model_copy(update={"status": "unavailable"}))
            continue
        if st.status == "unavailable":
            summary["restored"] += 1
        summary["updated"] += 1
        current = [p["id"] for p in obs.get("photos", [])]
        current_set = set(current)
        missing = [pid for pid in st.selected_photo_ids if pid not in current_set]
        known_photos = set(st.known_photo_ids)
        new_photos = [pid for pid in current if pid not in known_photos]
        if missing:
            summary["missing_photos"][str(st.id)] = missing
        if new_photos and st.known_photo_ids:
            summary["new_photos"][str(st.id)] = new_photos
        # Keep missing selected photos in the selection so the editor can flag
        # them; never substitute another photo silently.
        known_ids = current + [pid for pid in missing if pid not in current_set]
        if st.last_inat_name is not None and st.last_inat_name != obs.get("inat_name"):
            summary["name_changed"] += 1
        # Membership in sources that were not queried this time (disabled
        # ones) is unknown, so it is carried over unchanged.
        new_sources = membership.get(st.id, []) + [
            sid for sid in st.source_ids if sid not in source_results and sid not in membership.get(st.id, [])
        ]
        if st.source_ids and not new_sources:
            summary["no_longer_matching"] += 1
        states.append(st.model_copy(update={
            "status": "active",
            "known_photo_ids": known_ids,
            "last_inat_name": obs.get("inat_name") or "",
            # An observation that dropped out of every search keeps its old
            # membership so disabling those sources still hides it.
            "source_ids": new_sources or st.source_ids,
        }))

    new_ids = [
        oid for oid in pool
        if oid not in known and oid not in ignored and oid in observations
        and any(sid in enabled for sid in membership[oid])
    ]
    new_by_source = {sid: 0 for sid in order_ids}
    for oid in new_ids:
        for sid in membership[oid]:
            new_by_source[sid] += 1
    summary["new"] = len(new_ids)
    summary["new_ids"] = new_ids
    summary["new_by_source"] = new_by_source
    summary["membership"] = {str(oid): membership[oid] for oid in new_ids}
    return project.model_copy(update={"observations": states}), summary


# ---------------------------------------------------------------------------
# Edits
# ---------------------------------------------------------------------------

def add_observations(
    project: Project,
    ids: list[int],
    observations: dict[int, dict],
    membership: dict[int, list[str]],
    selected: dict[int, list[int]] | None = None,
    placement: str = "sorted",
    workspace: dict | None = None,
    max_observations: int | None = None,
) -> Project:
    """Add observations (first photo selected unless ``selected`` says otherwise).

    Raises ProjectError if the project would hold more than ``max_observations``,
    so it never grows past what a project file may contain.
    """
    from .sorting import insert_sorted

    selected = selected or {}
    existing = {s.id for s in project.observations}
    if max_observations is not None:
        adding = len({oid for oid in ids if oid not in existing and oid in observations})
        if len(existing) + adding > max_observations:
            room = max(0, max_observations - len(existing))
            raise ProjectError(
                f"A project can hold {max_observations:,} observations. This one has {len(existing):,}, "
                f"so {room:,} more can be added, not {adding:,}. Select fewer, or remove some first."
            )
    stamp = now_iso()
    new_states = []
    for oid in dict.fromkeys(ids):
        if oid in existing or oid not in observations:
            continue
        obs = observations[oid]
        photo_ids = [p["id"] for p in obs.get("photos", [])]
        chosen = selected.get(oid)
        available = set(photo_ids)
        chosen = photo_ids[:1] if chosen is None else [p for p in chosen if p in available]
        new_states.append(ObservationState(
            id=oid,
            selected_photo_ids=chosen,
            known_photo_ids=photo_ids,
            source_ids=list(membership.get(oid, [])),
            last_inat_name=obs.get("inat_name") or "",
            added_at=stamp,
        ))
    if not new_states:
        return project
    project = project.model_copy(update={"observations": project.observations + new_states})
    new_ids = [s.id for s in new_states]
    if placement == "append":
        order = project.order + new_ids
    else:
        order = insert_sorted(project.order, new_ids, observations, project, workspace)
    added = set(new_ids)
    ignored = [i for i in project.ignored_observation_ids if i not in added]
    return project.model_copy(update={"order": order, "ignored_observation_ids": ignored})


def ignore_observations(project: Project, ids: list[int]) -> Project:
    ignored = list(dict.fromkeys(project.ignored_observation_ids + [int(i) for i in ids]))
    return project.model_copy(update={"ignored_observation_ids": ignored})


def remove_source(project: Project, source_id: str) -> Project:
    """Remove a source. Observations it alone contributed leave the project;
    observations another source also matched stay."""
    if not any(s.id == source_id for s in project.sources):
        return project
    sources = [s for s in project.sources if s.id != source_id]
    keep, dropped = [], set()
    for st in project.observations:
        if source_id in st.source_ids:
            remaining = [s for s in st.source_ids if s != source_id]
            if not remaining:
                dropped.add(st.id)
                continue
            st = st.model_copy(update={"source_ids": remaining})
        keep.append(st)
    settings = project.settings
    if settings.title_photo and settings.title_photo.observation_id in dropped:
        settings = settings.model_copy(update={"title_photo": None})
    return project.model_copy(update={
        "sources": sources,
        "observations": keep,
        "order": [i for i in project.order if i not in dropped],
        "settings": settings,
    })
