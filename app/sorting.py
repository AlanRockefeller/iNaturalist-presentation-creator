"""Sorting, grouping and filtering of observations.

The unit being ordered is always an *observation*: its selected photos travel
together, so no sort or drag can split them. Order is a list of observation ids
stored in the project; sorting rewrites that list and nothing else, so photo
selections, overrides and settings survive any re-sort.
"""

from __future__ import annotations

import random
from typing import Iterable

from .inaturalist import GROUP_RANKS
from .models import Project, ObservationState

GROUP_LABELS = {
    "kingdom": "Kingdom", "phylum": "Phylum", "class": "Class", "order": "Order",
    "family": "Family", "genus": "Genus", "source": "Source",
}
UNCLASSIFIED = "Unclassified"
_HIGH = "￿"  # sorts missing values after real ones


def effective_name(state: ObservationState | None, obs: dict | None) -> str:
    if state is not None and state.overrides.scientific is not None:
        return state.overrides.scientific
    return (obs or {}).get("inat_name") or ""


def _text_key(value: str | None) -> tuple[int, str]:
    return (0, value.casefold()) if value else (1, _HIGH)


def taxonomic_key(obs: dict, state: ObservationState | None) -> tuple:
    ranks = obs.get("ranks") or {}
    return tuple(_text_key(ranks.get(r)) for r in GROUP_RANKS) + (_text_key(effective_name(state, obs)),)


def first_source_index(state: ObservationState | None, source_index: dict[str, int], active_only: set[str] | None = None) -> int:
    best = len(source_index) + 1
    for sid in (state.source_ids if state else []):
        if active_only is not None and sid not in active_only:
            continue
        if sid in source_index:
            best = min(best, source_index[sid])
    return best


def group_of(obs: dict | None, state: ObservationState | None, grouping: str, project: Project) -> tuple[tuple, str]:
    """(sort key for the group, display label). Missing values group as Unclassified."""
    if grouping == "none" or obs is None:
        return ((0,), "")
    if grouping == "source":
        index = {s.id: i for i, s in enumerate(project.sources)}
        enabled = {s.id for s in project.sources if s.enabled}
        i = first_source_index(state, index, enabled)
        if i >= len(project.sources):
            i = first_source_index(state, index)
        if i < len(project.sources):
            src = project.sources[i]
            return ((i,), src.label or src.url)
        return ((len(project.sources) + 1,), "Other observations")
    ranks = obs.get("ranks") or {}
    depth = GROUP_RANKS.index(grouping) + 1
    path = tuple(_text_key(ranks.get(r)) for r in GROUP_RANKS[:depth])
    label = ranks.get(grouping) or UNCLASSIFIED
    if not ranks.get(grouping):
        path = ((1, _HIGH),) * depth  # all unclassified together, at the end
    return (path, label)


def _inner_key(key: str, obs: dict, state: ObservationState | None, original_index: dict[int, tuple]):
    if key == "taxonomic":
        return taxonomic_key(obs, state)
    if key == "favorites":
        return (int(obs.get("faves_count") or 0),)
    if key == "observed_on":
        return _text_key(obs.get("observed_on"))
    if key == "created_at":
        return _text_key(obs.get("created_at"))
    if key == "name":
        return _text_key(effective_name(state, obs))
    if key == "original":
        return original_index.get(obs["id"], (10**9, 10**9))
    return (0,)


def original_order_index(project: Project, workspace: dict) -> dict[int, tuple]:
    """(source position, result position) of each observation's first appearance."""
    index: dict[int, tuple] = {}
    for si, src in enumerate(project.sources):
        for pos, oid in enumerate((workspace.get("source_results") or {}).get(src.id, [])):
            key = (si, pos)
            if oid not in index or key < index[oid]:
                index[oid] = key
    return index


def sort_ids(
    ids: Iterable[int],
    obs_map: dict[int, dict],
    project: Project,
    key: str,
    direction: str = "asc",
    workspace: dict | None = None,
    seed: int | None = None,
) -> list[int]:
    """Sort observation ids. Group order always comes first, ascending; ``direction``
    applies within groups. Placeholders for unavailable observations go last."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    states = {s.id: s for s in project.observations}
    grouping = project.settings.grouping
    original_index = original_order_index(project, workspace or {})
    known = [i for i in ids if i in obs_map]
    missing = [i for i in ids if i not in obs_map]
    if key == "custom":
        return ids

    # Bucket by group, then sort inside each bucket.
    buckets: dict[tuple, list[int]] = {}
    for oid in known:
        gkey, _ = group_of(obs_map[oid], states.get(oid), grouping, project)
        buckets.setdefault(gkey, []).append(oid)
    rng = random.Random(seed)
    out: list[int] = []
    for gkey in sorted(buckets):
        members = buckets[gkey]
        if key == "random":
            rng.shuffle(members)
        else:
            # Ties fall back to taxonomic order; reverse=True keeps the sort stable.
            members.sort(key=lambda i: taxonomic_key(obs_map[i], states.get(i)))
            if key != "taxonomic" or direction == "desc":
                members.sort(
                    key=lambda i: _inner_key(key, obs_map[i], states.get(i), original_index),
                    reverse=direction == "desc",
                )
        out.extend(members)
    return out + missing


def insert_sorted(
    current_order: list[int],
    new_ids: list[int],
    obs_map: dict[int, dict],
    project: Project,
    workspace: dict | None = None,
) -> list[int]:
    """Insert ``new_ids`` where the automatic sort would put them, without
    disturbing the (possibly hand-arranged) order of existing observations.

    Each new observation goes immediately after the nearest observation that
    precedes it in the full automatic sort.
    """
    sort = project.settings.sort
    key = sort.key if sort.key not in ("custom", "random") else sort.base_key
    direction = sort.direction if sort.key not in ("custom", "random") else sort.base_direction
    if key in ("custom", "random"):
        key, direction = "taxonomic", "asc"
    new_set = [i for i in dict.fromkeys(new_ids) if i not in set(current_order)]
    full = sort_ids(list(current_order) + new_set, obs_map, project, key, direction, workspace)
    order = list(current_order)
    new_lookup = set(new_set)
    last_existing: int | None = None
    after: dict[int | None, list[int]] = {}
    for oid in full:
        if oid in new_lookup:
            after.setdefault(last_existing, []).append(oid)
        else:
            last_existing = oid
    pending_front = after.pop(None, [])
    result: list[int] = list(pending_front)
    for oid in order:
        result.append(oid)
        result.extend(after.get(oid, []))
    return result


def passes_filters(obs: dict | None, state: ObservationState, project: Project) -> bool:
    """Whether an observation can contribute slides (ignoring photo selection)."""
    if obs is None or state.status != "active":
        return False
    if int(obs.get("faves_count") or 0) < project.settings.min_faves:
        return False
    if state.source_ids:
        enabled = {s.id for s in project.sources if s.enabled}
        if not any(sid in enabled for sid in state.source_ids):
            return False
    return True
