"""Pydantic models for project files and API requests.

The project file is the *only* persistent artifact of this app, and it lives on
the user's computer. It stores intent (sources, selections, overrides, order,
settings) and not iNaturalist metadata or photographs: loading a
project re-queries the saved sources so names, photos and counts are current.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROJECT_FORMAT = "dikarya-presentation"
SCHEMA_VERSION = 4
PROJECT_EXTENSION = ".dikarya-presentation.json"

SortKey = Literal["taxonomic", "favorites", "observed_on", "created_at", "name", "original", "random", "custom"]
Grouping = Literal["none", "kingdom", "phylum", "class", "order", "family", "genus", "source"]
SOURCE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,40}$"
Rotation = Literal[0, 90, 180, 270]
LineField = Literal["scientific", "common_name", "date", "location", "observer"]

_LIMITS = {"sources": 20, "observations": 10_000, "photos": 200, "ignored": 50_000}


class _Model(BaseModel):
    # Unknown keys are ignored so a newer minor addition does not break older
    # servers; anything we *do* read is strictly typed and bounded.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)


class Source(_Model):
    id: str = Field(pattern=SOURCE_ID_PATTERN)
    type: Literal["username", "url"]
    input: str = Field(default="", max_length=2000)
    username: Optional[str] = Field(default=None, max_length=40)
    url: str = Field(max_length=2500)
    label: str = Field(default="", max_length=120)
    enabled: bool = True


class AnnotationSettings(_Model):
    scientific: bool = True
    common_name: bool = False
    date: bool = False
    location: bool = False
    # None = automatic: off for username-only projects, on if any source is a URL.
    observer: Optional[bool] = None


class SortSettings(_Model):
    key: SortKey = "taxonomic"
    direction: Literal["asc", "desc"] = "asc"
    # The automatic sort a "custom" (hand-dragged) order was derived from; used
    # to place newly added observations.
    base_key: SortKey = "taxonomic"
    base_direction: Literal["asc", "desc"] = "asc"


class TitlePhoto(_Model):
    observation_id: int = Field(gt=0)
    photo_id: int = Field(gt=0)
    position: Literal["center", "top", "bottom"] = "center"


class PresentationSettings(_Model):
    title: str = Field(default="", max_length=200)
    presenter: str = Field(default="", max_length=200)
    background: Literal["black", "white"] = "black"
    title_photo: Optional[TitlePhoto] = None
    sort: SortSettings = Field(default_factory=SortSettings)
    grouping: Grouping = "family"
    dividers: bool = False
    min_faves: int = Field(default=0, ge=0, le=1_000_000)
    annotations: AnnotationSettings = Field(default_factory=AnnotationSettings)
    speaker_notes: bool = True


class AnnotationOverrides(_Model):
    """None = use the live iNaturalist value; "" = hide that line for this observation."""

    scientific: Optional[str] = Field(default=None, max_length=300)
    common_name: Optional[str] = Field(default=None, max_length=300)
    date: Optional[str] = Field(default=None, max_length=300)
    location: Optional[str] = Field(default=None, max_length=300)
    observer: Optional[str] = Field(default=None, max_length=300)

    @field_validator("*", mode="before")
    @classmethod
    def _no_control_chars(cls, v):
        if isinstance(v, str):
            return "".join(ch if ch >= " " or ch == "\t" else " " for ch in v)
        return v


class ObservationState(_Model):
    id: int = Field(gt=0)
    selected_photo_ids: list[int] = Field(default_factory=list, max_length=_LIMITS["photos"])
    known_photo_ids: list[int] = Field(default_factory=list, max_length=_LIMITS["photos"])
    overrides: AnnotationOverrides = Field(default_factory=AnnotationOverrides)
    # Clockwise rotation in degrees chosen by the user, by photo id. 0 is not stored.
    rotations: dict[int, Rotation] = Field(default_factory=dict, max_length=_LIMITS["photos"])
    # Include (true) or leave out (false) a slide line for this observation,
    # whatever Presentation Settings say. Missing = follow the setting.
    show_lines: dict[LineField, bool] = Field(default_factory=dict)
    # The user's order for this observation's photos (ids). Photos not listed
    # follow in iNaturalist's order. Empty = iNaturalist's order.
    photo_order: list[int] = Field(default_factory=list, max_length=_LIMITS["photos"])
    source_ids: list[str] = Field(default_factory=list, max_length=_LIMITS["sources"])
    status: Literal["active", "unavailable"] = "active"
    last_inat_name: Optional[str] = Field(default=None, max_length=300)
    added_at: Optional[str] = Field(default=None, max_length=40)

    @field_validator("source_ids")
    @classmethod
    def _source_ids(cls, v):
        import re

        return [s for s in v if isinstance(s, str) and re.match(SOURCE_ID_PATTERN, s)]


class Project(_Model):
    format: Literal["dikarya-presentation"] = PROJECT_FORMAT
    schema_version: int = SCHEMA_VERSION
    saved_at: Optional[str] = Field(default=None, max_length=40)
    app_version: Optional[str] = Field(default=None, max_length=40)
    sources: list[Source] = Field(default_factory=list, max_length=_LIMITS["sources"])
    settings: PresentationSettings = Field(default_factory=PresentationSettings)
    observations: list[ObservationState] = Field(default_factory=list, max_length=_LIMITS["observations"])
    order: list[int] = Field(default_factory=list, max_length=_LIMITS["observations"])
    ignored_observation_ids: list[int] = Field(default_factory=list, max_length=_LIMITS["ignored"])


# ---------------------------------------------------------------------------
# API request bodies
# ---------------------------------------------------------------------------

class ParseSourceRequest(_Model):
    url: str = Field(default="", max_length=2000)
    username: str = Field(default="", max_length=60)


class LoadRequest(_Model):
    project: Project
    workspace_id: Optional[str] = Field(default=None, max_length=64)
    # Only re-query these sources (e.g. one just added); None = all enabled sources.
    refresh_source_ids: Optional[list[str]] = Field(default=None, max_length=_LIMITS["sources"])


class ProjectRequest(_Model):
    project: Project
    workspace_id: str = Field(max_length=64)


class SortRequest(ProjectRequest):
    key: SortKey
    direction: Literal["asc", "desc"] = "asc"
    seed: Optional[int] = None


class AddObservationsRequest(ProjectRequest):
    observation_ids: list[int] = Field(max_length=_LIMITS["observations"])
    # Per-observation photo choice from the review dialog; missing = first photo only.
    selected_photo_ids: dict[str, list[int]] = Field(default_factory=dict)
    placement: Literal["sorted", "append"] = "sorted"
    ignore_ids: list[int] = Field(default_factory=list, max_length=_LIMITS["observations"])
