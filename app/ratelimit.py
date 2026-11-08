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
        self._hold = 0.0  # no request may start before this (backoff after a 429)
        self._generation = 0  # bumped when a penalty voids every pending reservation

    def wait(self) -> float:
        waited = 0.0
        while True:
            with self._lock:
                generation = self._generation
                now = self._clock()
                start = max(now, self._next, self._hold)
                self._next = start + self.min_interval
            delay = start - now
            if delay > 0:
                self._sleep(delay)
                waited += delay
            with self._lock:
                # A penalty that arrived while this thread slept also applies to it:
                # queue again behind the new hold instead of going at the old time.
                if generation == self._generation:
                    return waited

    def penalize(self, seconds: float) -> None:
        """Push every request back, including ones already waiting, e.g. after a
        429 with Retry-After."""
        with self._lock:
            # At least one interval, so the queue restarting at the hold still keeps
            # the spacing from a request that has just started.
            hold = self._clock() + max(self.min_interval, seconds)
            if hold > self._hold:
                # Every waiter queues again from the new hold, in the order it wakes,
                # so none is pushed behind reservations made before the penalty.
                self._hold = self._next = hold
                self._generation += 1


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
    """Rolling hour/day byte counters kept in one-minute buckets.

    A generation reserves its estimated download up front, so two decks that
    start together cannot both pass the check and then run out halfway.
    """

    def __init__(self, hourly: int, daily: int, path: Path | None = None, clock=time.time):
        self.hourly = int(hourly)
        self.daily = int(daily)
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[int, int] = {}
        self._reserved = 0
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
        """Bytes left this hour and today, not counting reservations."""
        with self._lock:
            hour, day = self._totals()
            return max(0, self.hourly - hour), max(0, self.daily - day)

    @staticmethod
    def _refuse_if(expected: int, hour_left: int, day_left: int) -> None:
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

    def check(self, expected: int = 0) -> None:
        self._refuse_if(expected, *self.remaining())

    def _unreserved(self) -> tuple[int, int]:
        """Bytes left this hour and today that no deck has reserved, never
        below zero (finished downloads can overshoot). The caller holds the lock."""
        hour, day = self._totals()
        return max(0, self.hourly - hour - self._reserved), max(0, self.daily - day - self._reserved)

    def reserve(self, expected: int) -> "Reservation":
        """Set aside ``expected`` bytes, or raise BudgetExceeded if other
        downloads and reservations leave too little. Reserving nothing (a deck
        whose photos are all cached) always works."""
        with self._lock:
            self._refuse_if(expected, *self._unreserved())
            self._reserved += expected
        return Reservation(self, expected)

    def allow_download(self, reservation: "Reservation | None" = None) -> None:
        """Raise BudgetExceeded unless one more download may start: inside the
        deck's own reservation, or else only from allowance no other deck has
        reserved, so a deck that downloads more than it estimated cannot use
        up another deck's share."""
        with self._lock:
            hour, day = self._totals()
            # Never past the real allowance, reserved or not: a download that
            # overshot it must not let reserved downloads add to the overshoot.
            self._refuse_if(1, self.hourly - hour, self.daily - day)
            if reservation is not None and reservation.left > 0:
                return
            self._refuse_if(1, *self._unreserved())

    def _use(self, reservation: "Reservation", nbytes: int) -> None:
        with self._lock:  # downloads run in several threads
            self._use_locked(reservation, nbytes)

    def _use_locked(self, reservation: "Reservation", nbytes: int) -> None:
        take = min(reservation.left, max(0, int(nbytes)))
        reservation.left -= take
        self._reserved = max(0, self._reserved - take)

    def record(self, nbytes: int, reservation: "Reservation | None" = None) -> None:
        """Count downloaded bytes and take them from ``reservation`` in one step,
        so another deck checking its allowance never sees them counted twice."""
        with self._lock:
            self._add_locked(nbytes)
            if reservation is not None:
                self._use_locked(reservation, nbytes)

    def add(self, nbytes: int) -> None:
        with self._lock:
            self._add_locked(nbytes)

    def _add_locked(self, nbytes: int) -> None:
        """The caller holds the lock."""
        minute = int(self._clock() // 60)
        self._buckets[minute] = self._buckets.get(minute, 0) + int(nbytes)
        now = time.monotonic()
        if self.path and now - self._last_flush > 10:
            self._last_flush = now
            try:
                _atomic_write_json(self.path, self._buckets)
            except OSError:
                pass

    def flush(self) -> None:
        if not self.path:
            return
        with self._lock:
            try:
                _atomic_write_json(self.path, self._buckets)
            except OSError:
                pass


class Reservation:
    """Bytes set aside for one generation. Downloads use it up as they are
    counted; ``release()`` returns what is left."""

    def __init__(self, budget: ByteBudget, nbytes: int):
        self._budget = budget
        self.left = int(nbytes)

    def consume(self, nbytes: int) -> None:
        self._budget._use(self, nbytes)

    def release(self) -> None:
        self._budget._use(self, self.left)
