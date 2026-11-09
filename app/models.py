"""Pydantic models for project files and API requests.

The project file is the *only* persistent artifact of this app, and it lives on
the user's computer. It stores intent (sources, selections, overrides, order,
settings) and not iNaturalist metadata or photographs: loading a
project re-queries the saved sources so names, photos and counts are current.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

PROJECT_FORMAT = "dikarya-presentation"
SCHEMA_VERSION = 7
PROJECT_EXTENSION = ".dikarya-presentation.json"

SortKey = Literal["taxonomic", "favorites", "observed_on", "created_at", "name", "original", "random", "custom"]
Grouping = Literal["none", "kingdom", "phylum", "class", "order", "family", "genus", "source"]
SOURCE_ID_PATTERN = r"^[A-Za-z0-9_-]{1,40}$"
Rotation = Literal[0, 90, 180, 270]
LineField = Literal["scientific", "common_name", "date", "location", "observer"]
# Photo licenses as iNaturalist names them; "arr" is "all rights reserved"
# (a photo with no license).
LicenseCode = Literal["cc0", "cc-by", "cc-by-sa", "cc-by-nd", "cc-by-nc", "cc-by-nc-sa", "cc-by-nc-nd", "arr"]
DEFAULT_LICENSES = ["cc0", "cc-by", "cc-by-sa", "cc-by-nd"]  # allow commercial use
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,39}$")
MAX_RELATED_TAXA = 1000  # species remembered per "more photos" source

_LIMITS = {"sources": 20, "observations": 10_000, "photos": 200, "ignored": 50_000}
MAX_PHOTOS = _LIMITS["photos"]    # photos remembered per observation
MAX_IGNORED = _LIMITS["ignored"]  # ignored observation ids per project
MAX_NOTES_CHARS = 4000            # presenter notes per observation

# Characters that cannot be written to XML (so not to a .pptx) or to UTF-8.
_UNENCODABLE = re.compile("[\ud800-\udfff￾￿]")
# Control characters other than tab, for example a line break pasted from
# PowerPoint or Word (vertical tab).
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
# The same, but line breaks stay: presenter notes have one point per line.
_CONTROL_MULTILINE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def strip_unencodable(value):
    return _UNENCODABLE.sub("", value) if isinstance(value, str) else value


def clean_text(value):
    """Free text that may end up on a slide: drop characters that cannot be
    stored, and turn control characters into spaces."""
    if isinstance(value, str):
        return _CONTROL.sub(" ", _UNENCODABLE.sub("", value))
    return value


def clean_multiline(value):
    """Like clean_text, but line breaks are kept (as \\n)."""
    if isinstance(value, str):
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        return _CONTROL_MULTILINE.sub(" ", _UNENCODABLE.sub("", value))
    return value


Text = Annotated[str, BeforeValidator(clean_text)]
MultilineText = Annotated[str, BeforeValidator(clean_multiline)]
Encodable = Annotated[str, BeforeValidator(strip_unencodable)]


class _Model(BaseModel):
    # Unknown keys are ignored so a newer minor addition does not break older
    # servers; anything we *do* read is strictly typed and bounded.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)


class RelatedSource(_Model):
    """A "more photos of these species" source: which photos, and the species
    searched so far. How to search (licenses, how many) is in
    PresentationSettings.related, so changing it affects the next search."""

    whose: Literal["mine", "others"]
    taxon_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list, max_length=MAX_RELATED_TAXA)

    @field_validator("taxon_ids")
    @classmethod
    def _once(cls, v):
        return list(dict.fromkeys(v))


class Source(_Model):
    id: str = Field(pattern=SOURCE_ID_PATTERN)
    # "username": the presenter's own photos (photo credits start off).
    # "url": may include other people's photos (credits start on).
    # Since schema 5 this is the user's choice and is kept as saved.
    # "related" (schema 6): more photos of species already in the project,
    # searched only when the user asks (see app/related.py).
    type: Literal["username", "url", "related"]
    input: Encodable = Field(default="", max_length=2000)
    username: Optional[str] = Field(default=None, max_length=40)
    # A related source's link lists its species, so it can be longer.
    url: Encodable = Field(max_length=12_000)
    label: Text = Field(default="", max_length=120)
    enabled: bool = True
    related: Optional[RelatedSource] = None


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


class RelatedMine(_Model):
    per_taxon: int = Field(default=20, ge=1, le=200)
    min_faves: int = Field(default=0, ge=0, le=1_000_000)


class RelatedOthers(_Model):
    licenses: list[LicenseCode] = Field(default_factory=lambda: list(DEFAULT_LICENSES), min_length=1, max_length=8)
    per_taxon: int = Field(default=5, ge=1, le=200)
    min_faves: int = Field(default=1, ge=0, le=1_000_000)
    research_only: bool = True

    @field_validator("licenses")
    @classmethod
    def _once(cls, v):
        return list(dict.fromkeys(v))


class RelatedSettings(_Model):
    """How "More of mine" / "More from others" search (set in the More photos
    dialog on Select & Organize)."""

    # The presenter's iNaturalist login: needed for "mine", left out of
    # "others", and never credited on slides. "" = not given.
    username: str = Field(default="", max_length=40)
    # Show the two buttons on each observation card.
    buttons: bool = False
    mine: RelatedMine = Field(default_factory=RelatedMine)
    others: RelatedOthers = Field(default_factory=RelatedOthers)

    @field_validator("username", mode="before")
    @classmethod
    def _login(cls, v):
        v = (v or "").strip().lstrip("@") if isinstance(v, str) else v
        if isinstance(v, str) and v and (not LOGIN_RE.match(v) or v.isdigit()):
            raise ValueError("not a valid iNaturalist username")
        return v


class PresentationSettings(_Model):
    title: Text = Field(default="", max_length=200)
    presenter: Text = Field(default="", max_length=200)
    background: Literal["black", "white"] = "black"
    title_photo: Optional[TitlePhoto] = None
    sort: SortSettings = Field(default_factory=SortSettings)
    grouping: Grouping = "family"
    dividers: bool = False
    min_faves: int = Field(default=0, ge=0, le=1_000_000)
    annotations: AnnotationSettings = Field(default_factory=AnnotationSettings)
    speaker_notes: bool = True
    related: RelatedSettings = Field(default_factory=RelatedSettings)
    # Each observation's presenter notes (ObservationState.notes) in the deck.
    presenter_notes: bool = True


class AnnotationOverrides(_Model):
    """None = use the live iNaturalist value; "" = hide that line for this observation."""

    scientific: Optional[Text] = Field(default=None, max_length=300)
    common_name: Optional[Text] = Field(default=None, max_length=300)
    date: Optional[Text] = Field(default=None, max_length=300)
    location: Optional[Text] = Field(default=None, max_length=300)
    observer: Optional[Text] = Field(default=None, max_length=300)


class ObservationState(_Model):
    id: int = Field(gt=0)
    selected_photo_ids: list[int] = Field(default_factory=list, max_length=MAX_PHOTOS)
    known_photo_ids: list[int] = Field(default_factory=list, max_length=MAX_PHOTOS)
    overrides: AnnotationOverrides = Field(default_factory=AnnotationOverrides)
    # Clockwise rotation in degrees chosen by the user, by photo id. 0 is not stored.
    rotations: dict[int, Rotation] = Field(default_factory=dict, max_length=MAX_PHOTOS)
    # Include (true) or leave out (false) a slide line for this observation,
    # whatever Presentation Settings say. Missing = follow the setting.
    show_lines: dict[LineField, bool] = Field(default_factory=dict)
    # The user's order for this observation's photos (ids). Photos not listed
    # follow in iNaturalist's order. Empty = iNaturalist's order.
    photo_order: list[int] = Field(default_factory=list, max_length=MAX_PHOTOS)
    source_ids: list[str] = Field(default_factory=list, max_length=_LIMITS["sources"])
    status: Literal["active", "unavailable"] = "active"
    last_inat_name: Optional[Text] = Field(default=None, max_length=300)
    added_at: Optional[str] = Field(default=None, max_length=40)
    # Presenter notes for this observation's slides, typed by the user or
    # written by Claude (app/notes.py), and the name they were written for, so
    # a changed name can be pointed out. None = no notes.
    notes: Optional[MultilineText] = Field(default=None, max_length=MAX_NOTES_CHARS)
    notes_name: Optional[Text] = Field(default=None, max_length=300)

    @field_validator("source_ids")
    @classmethod
    def _source_ids(cls, v):
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
    ignored_observation_ids: list[int] = Field(default_factory=list, max_length=MAX_IGNORED)


# ---------------------------------------------------------------------------
# API request bodies
# ---------------------------------------------------------------------------

class ParseSourceRequest(_Model):
    url: Encodable = Field(default="", max_length=2000)
    username: Encodable = Field(default="", max_length=60)


class LoadRequest(_Model):
    project: Project
    workspace_id: Optional[str] = Field(default=None, max_length=64)
    # Only re-query these sources (e.g. one just added); None = all enabled sources.
    refresh_source_ids: Optional[list[str]] = Field(default=None, max_length=_LIMITS["sources"])
    # "More photos" sources in refresh_source_ids search only these species this
    # time (one card's button, or the species listed); None = all of theirs.
    related_taxa: Optional[list[Annotated[int, Field(gt=0)]]] = Field(default=None, max_length=MAX_RELATED_TAXA)


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
    selected_photo_ids: dict[str, list[int]] = Field(default_factory=dict, max_length=_LIMITS["observations"])
    # "after": right after after_id (the card whose "More photos" button was used).
    placement: Literal["sorted", "append", "after"] = "sorted"
    after_id: Optional[int] = Field(default=None, gt=0)
    ignore_ids: list[int] = Field(default_factory=list, max_length=_LIMITS["observations"])


class NotesRequest(ProjectRequest):
    # Observations to write presenter notes for; None = every observation with
    # a slide in the presentation.
    observation_ids: Optional[list[int]] = Field(default=None, max_length=_LIMITS["observations"])
    # False: skip observations that already have notes.
    replace: bool = False
    # Let Claude search the web to check its facts (slower).
    web_search: bool = False
