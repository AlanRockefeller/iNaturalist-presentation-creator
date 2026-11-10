"""Runtime configuration, read once from the environment.

Every limit that protects the public site or iNaturalist lives here so it can be
tuned in the systemd unit without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

GIB = 1024 ** 3
MIB = 1024 ** 2


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    # Temporary storage: workspaces, cached originals, generated decks, taxa cache.
    work_dir: Path = field(default_factory=lambda: Path(os.environ.get("PRESENTATIONS_WORK_DIR", "var")).resolve())

    api_base: str = "https://api.inaturalist.org"
    user_agent: str = f"DikaryaPresentations/{__version__} (+https://presentations.dikarya.us; contact via dikarya.us)"

    # iNaturalist API limits: ~1 request/second process-wide, ~10k/day.
    api_min_interval: float = field(default_factory=lambda: _env_float("PRESENTATIONS_API_MIN_INTERVAL", 1.0))
    api_daily_budget: int = field(default_factory=lambda: _env_int("PRESENTATIONS_API_DAILY_BUDGET", 8500))
    # The most of that budget one client (an IPv4 address or an IPv6 /64) may
    # use per UTC day, so a single visitor cannot use up the day for everyone.
    api_client_daily_share: float = field(default_factory=lambda: _env_float("PRESENTATIONS_API_CLIENT_SHARE", 0.25))
    api_timeout: float = 30.0
    api_max_retries: int = 4

    # Media limits: iNat may block above 5 GB/hour or 24 GB/day.
    media_hourly_bytes: int = field(default_factory=lambda: _env_int("PRESENTATIONS_MEDIA_HOURLY_BYTES", 4 * GIB))
    media_daily_bytes: int = field(default_factory=lambda: _env_int("PRESENTATIONS_MEDIA_DAILY_BYTES", 20 * GIB))
    media_concurrency: int = 3
    media_timeout: float = 60.0
    max_image_bytes: int = 40 * MIB
    # Short-lived cache of downloaded originals so a regenerate after a typo fix
    # does not re-download every photo. Not permanent storage.
    media_cache_ttl_seconds: int = field(default_factory=lambda: _env_int("PRESENTATIONS_MEDIA_CACHE_TTL", 2 * 3600))
    media_cache_max_bytes: int = field(default_factory=lambda: _env_int("PRESENTATIONS_MEDIA_CACHE_MAX_BYTES", 8 * GIB))

    # Input size limits.
    max_sources: int = 20
    max_observations_per_source: int = 10_000  # iNat's page/per_page ceiling
    max_observations_per_project: int = field(default_factory=lambda: _env_int("PRESENTATIONS_MAX_OBSERVATIONS", 10_000))
    max_photos_per_observation: int = 200  # at most models.MAX_PHOTOS
    max_image_slides: int = field(default_factory=lambda: _env_int("PRESENTATIONS_MAX_IMAGE_SLIDES", 2000))
    # Species in one "more photos" search (other people's photos cost about one
    # request per species). At most models.MAX_RELATED_TAXA.
    max_related_taxa: int = field(default_factory=lambda: _env_int("PRESENTATIONS_MAX_RELATED_TAXA", 500))
    max_body_bytes: int = 8 * MIB

    # Jobs.
    max_concurrent_loads: int = 3
    max_queued_loads: int = 12  # running plus waiting, from all clients
    max_concurrent_generations: int = field(default_factory=lambda: _env_int("PRESENTATIONS_MAX_GENERATIONS", 2))
    max_queued_generations: int = 6
    max_active_jobs_per_client: int = 2
    generation_timeout_seconds: int = 60 * 60
    load_timeout_seconds: int = 20 * 60
    job_ttl_seconds: int = 2 * 3600
    workspace_ttl_seconds: int = 24 * 3600
    min_free_disk_bytes: int = 3 * GIB

    # Presenter notes written by the Claude Code CLI (app/notes.py). On only
    # when the CLI, a token (from `claude setup-token`) and an access code are
    # all set; the deploy script keeps the last two in /etc/presentations/notes.env.
    notes_cli: str = field(default_factory=lambda: os.environ.get(
        "PRESENTATIONS_NOTES_CLI", "/usr/local/lib/presentations/claude"))
    notes_token: str = field(default_factory=lambda: os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip(), repr=False)
    notes_access_code: str = field(
        default_factory=lambda: os.environ.get("PRESENTATIONS_NOTES_ACCESS_CODE", "").strip(), repr=False)
    notes_model: str = field(default_factory=lambda: os.environ.get("PRESENTATIONS_NOTES_MODEL", "claude-opus-5-5"))
    notes_effort: str = field(default_factory=lambda: os.environ.get("PRESENTATIONS_NOTES_EFFORT", "medium"))
    # Names (species or other taxa) Claude may write notes for per UTC day, and per job.
    notes_daily_budget: int = field(default_factory=lambda: _env_int("PRESENTATIONS_NOTES_DAILY_BUDGET", 1500))
    notes_max_per_job: int = field(default_factory=lambda: _env_int("PRESENTATIONS_NOTES_MAX_PER_JOB", 1000))
    notes_batch_size: int = 8       # names per Claude call
    notes_concurrency: int = 2      # Claude calls at once, in one job
    notes_call_timeout: float = 240.0
    # "Search the web" lets Claude use Claude Code's WebSearch tool (run by
    # Anthropic; WebFetch, which would fetch pages from this server, stays off).
    notes_web_search: bool = field(default_factory=lambda: _env_int("PRESENTATIONS_NOTES_WEB_SEARCH", 1) == 1)
    notes_web_batch_size: int = 4   # names per call when searching the web
    notes_web_call_timeout: float = 600.0
    notes_timeout_seconds: int = 2 * 3600
    max_queued_notes: int = 3       # running plus waiting notes jobs, from all clients

    def notes_configured(self) -> bool:
        return bool(self.notes_token and self.notes_access_code and os.access(self.notes_cli, os.X_OK))

    @property
    def workspace_dir(self) -> Path:
        return self.work_dir / "workspaces"

    @property
    def media_cache_dir(self) -> Path:
        return self.work_dir / "media-cache"

    @property
    def jobs_dir(self) -> Path:
        return self.work_dir / "jobs"

    @property
    def state_dir(self) -> Path:
        return self.work_dir / "state"

    @property
    def claude_dir(self) -> Path:
        """Home and config directory of the Claude Code CLI (app/notes.py)."""
        return self.work_dir / "claude"

    def ensure_dirs(self) -> None:
        for d in (self.work_dir, self.workspace_dir, self.media_cache_dir, self.jobs_dir, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Used by tests to install an isolated configuration."""
    global _settings
    _settings = settings
