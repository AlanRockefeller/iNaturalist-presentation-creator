"""Runtime configuration, read once from the environment.

Every limit that protects the public site or iNaturalist lives here so it can be
tuned in the systemd unit without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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
    user_agent: str = "DikaryaPresentations/1.0 (+https://presentations.dikarya.us; contact via dikarya.us)"

    # iNaturalist API limits: ~1 request/second process-wide, ~10k/day.
    api_min_interval: float = field(default_factory=lambda: _env_float("PRESENTATIONS_API_MIN_INTERVAL", 1.0))
    api_daily_budget: int = field(default_factory=lambda: _env_int("PRESENTATIONS_API_DAILY_BUDGET", 8500))
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
    max_photos_per_observation: int = 200
    max_image_slides: int = field(default_factory=lambda: _env_int("PRESENTATIONS_MAX_IMAGE_SLIDES", 2000))
    max_body_bytes: int = 8 * MIB

    # Jobs.
    max_concurrent_loads: int = 3
    max_concurrent_generations: int = field(default_factory=lambda: _env_int("PRESENTATIONS_MAX_GENERATIONS", 2))
    max_queued_generations: int = 6
    max_active_jobs_per_client: int = 2
    generation_timeout_seconds: int = 60 * 60
    load_timeout_seconds: int = 20 * 60
    job_ttl_seconds: int = 2 * 3600
    workspace_ttl_seconds: int = 24 * 3600
    min_free_disk_bytes: int = 3 * GIB

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
