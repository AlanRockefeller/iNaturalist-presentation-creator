"""Process-wide throttles that keep this site inside iNaturalist's published limits.

* ``RateLimiter``      ~1 API request per second, shared by every user and job.
* ``DailyBudget``      API requests per UTC day (iNat asks for ~10k/day).
* ``ByteBudget``       media bytes per rolling hour and day (iNat may block a host
                       above 5 GB/hour or 24 GB/day).

The service runs a single Uvicorn worker, so in-process state is global. The
budgets persist to small JSON files so a restart does not reset them.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class BudgetExceeded(Exception):
    """Raised when a daily/hourly budget would be exceeded. Message is user-safe."""


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class RateLimiter:
    """Spaces calls at least ``min_interval`` seconds apart across all threads."""

    def __init__(self, min_interval: float, clock=time.monotonic, sleep=time.sleep):
        self.min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> float:
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self.min_interval
        delay = start - now
        if delay > 0:
            self._sleep(delay)
        return delay

    def penalize(self, seconds: float) -> None:
        """Push every future request back, e.g. after a 429 with Retry-After."""
        with self._lock:
            self._next = max(self._next, self._clock() + max(0.0, seconds))


class DailyBudget:
    def __init__(self, limit: int, path: Path | None = None, today=None):
        self.limit = int(limit)
        self.path = path
        self._today = today or (lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        self._lock = threading.Lock()
        self._day = self._today()
        self._count = 0
        self._last_flush = 0.0
        if path and path.exists():
            try:
                data = json.loads(path.read_text())
                if data.get("day") == self._day:
                    self._count = int(data.get("count", 0))
            except (OSError, ValueError, TypeError):
                pass

    def _roll(self) -> None:
        day = self._today()
        if day != self._day:
            self._day, self._count = day, 0

    def remaining(self) -> int:
        with self._lock:
            self._roll()
            return max(0, self.limit - self._count)

    def consume(self, n: int = 1) -> None:
        with self._lock:
            self._roll()
            if self._count + n > self.limit:
                raise BudgetExceeded(
                    "This site has reached its daily iNaturalist request allowance. "
                    "Please try again tomorrow (UTC)."
                )
            self._count += n
            now = time.monotonic()
            if self.path and now - self._last_flush > 5:
                self._last_flush = now
                try:
                    _atomic_write_json(self.path, {"day": self._day, "count": self._count})
                except OSError:
                    pass

    def flush(self) -> None:
        if not self.path:
            return
        with self._lock:
            try:
                _atomic_write_json(self.path, {"day": self._day, "count": self._count})
            except OSError:
                pass


class ByteBudget:
    """Rolling hour/day byte counters kept in one-minute buckets."""

    def __init__(self, hourly: int, daily: int, path: Path | None = None, clock=time.time):
        self.hourly = int(hourly)
        self.daily = int(daily)
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[int, int] = {}
        self._last_flush = 0.0
        if path and path.exists():
            try:
                raw = json.loads(path.read_text())
                self._buckets = {int(k): int(v) for k, v in raw.items()}
            except (OSError, ValueError, TypeError, AttributeError):
                self._buckets = {}

    def _prune(self, minute: int) -> None:
        cutoff = minute - 24 * 60
        for k in [k for k in self._buckets if k <= cutoff]:
            del self._buckets[k]

    def _totals(self) -> tuple[int, int]:
        minute = int(self._clock() // 60)
        self._prune(minute)
        hour = sum(v for k, v in self._buckets.items() if k > minute - 60)
        day = sum(self._buckets.values())
        return hour, day

    def remaining(self) -> tuple[int, int]:
        with self._lock:
            hour, day = self._totals()
            return max(0, self.hourly - hour), max(0, self.daily - day)

    def check(self, expected: int = 0) -> None:
        hour_left, day_left = self.remaining()
        if expected > day_left:
            raise BudgetExceeded(
                "This site has reached its daily photo-download allowance from iNaturalist. "
                "Please try again tomorrow, or make a smaller presentation."
            )
        if expected > hour_left:
            raise BudgetExceeded(
                "This site has downloaded a lot of photos from iNaturalist in the last hour. "
                "Please try again in an hour, or make a smaller presentation."
            )

    def add(self, nbytes: int) -> None:
        with self._lock:
            minute = int(self._clock() // 60)
            self._buckets[minute] = self._buckets.get(minute, 0) + int(nbytes)
            now = time.monotonic()
            if self.path and now - self._last_flush > 10:
                self._last_flush = now
                try:
                    _atomic_write_json(self.path, self._buckets)
                except OSError:
                    pass
