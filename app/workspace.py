"""Temporary server-side store of normalized iNaturalist metadata.

A workspace holds what the last load fetched (observations + per-source result
lists) so sorting, preview and generation work from trusted, server-derived data
without the browser re-uploading megabytes of metadata. Workspaces are gzipped
JSON files that expire ``workspace_ttl_seconds`` after they were last used (the
file's modification time records the last use); they hold no user edits and
no photographs.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .diagnostics import get_logger

log = get_logger(__name__)

WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


class WorkspaceExpired(Exception):
    pass


class WorkspaceStore:
    def __init__(self, directory: Path, ttl_seconds: int, memory_items: int = 6):
        self.directory = directory
        self.ttl = ttl_seconds
        self._mem: OrderedDict[str, dict] = OrderedDict()
        self._mem_items = memory_items
        self._lock = threading.Lock()

    def _path(self, wid: str) -> Path:
        if not isinstance(wid, str) or not WORKSPACE_ID_RE.match(wid):
            raise WorkspaceExpired()
        return self.directory / f"{wid}.json.gz"

    def create(self, observations: dict[int, dict], source_results: dict[str, list[int]]) -> str:
        wid = secrets.token_urlsafe(24)
        data = {
            "created": time.time(),
            "observations": {str(k): v for k, v in observations.items()},
            "source_results": source_results,
        }
        path = self._path(wid)
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=5) as gz:
                gz.write(json.dumps(data, separators=(",", ":")).encode())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        ws = self._inflate(data)
        ws["used"] = data["created"]
        self._remember(wid, ws)
        return wid

    @staticmethod
    def _inflate(data: dict) -> dict:
        return {
            "created": data.get("created", 0),
            "observations": {int(k): v for k, v in (data.get("observations") or {}).items()},
            "source_results": {k: [int(i) for i in v] for k, v in (data.get("source_results") or {}).items()},
        }

    def _remember(self, wid: str, ws: dict) -> None:
        with self._lock:
            self._mem[wid] = ws
            self._mem.move_to_end(wid)
            while len(self._mem) > self._mem_items:
                self._mem.popitem(last=False)

    def get(self, wid: str) -> dict:
        """The workspace, if it was used within the TTL. Each use extends it."""
        path = self._path(wid)
        while True:
            with self._lock:
                ws = self._mem.get(wid)
            loaded = None
            if ws is None:
                # Read outside the lock (it can be megabytes); published below.
                try:
                    with gzip.open(path, "rb") as fh:
                        loaded = self._inflate(json.loads(fh.read()))
                except (OSError, ValueError, EOFError):
                    raise WorkspaceExpired() from None
            # Published, checked and extended under the lock, which cleanup()
            # also takes: a copy another request loaded and renewed meanwhile is
            # kept (not replaced by this one), two uses at once cannot put the
            # time back, and cleanup cannot delete the file between check and
            # renewal.
            with self._lock:
                ws = self._mem.get(wid)
                if ws is None:
                    if loaded is None:
                        continue  # dropped from memory meanwhile: read the file (outside the lock)
                    try:
                        # Its last use is the file's time now, which a request
                        # that renewed it meanwhile may have moved on.
                        loaded["used"] = path.stat().st_mtime
                    except OSError:
                        raise WorkspaceExpired() from None
                    ws = self._mem[wid] = loaded
                self._mem.move_to_end(wid)
                now = time.time()
                if now - ws["used"] > self.ttl:
                    raise WorkspaceExpired()
                try:
                    # The file's time is the last use, for restarts and for cleanup().
                    os.utime(path)
                except FileNotFoundError:
                    # Its file is gone, so it could not outlive this process:
                    # treat it as expired rather than alive in memory only.
                    self._mem.pop(wid, None)
                    raise WorkspaceExpired() from None
                except OSError as exc:
                    # Still in use: keep it alive in memory (cleanup() checks
                    # that too), and say so, since a restart would lose it sooner.
                    log.warning("event=workspace.touch_failed error_type=%s", type(exc).__name__)
                ws["used"] = max(ws["used"], now)
                # Trim last, so even a store that keeps nothing in memory works.
                while len(self._mem) > self._mem_items:
                    self._mem.popitem(last=False)
                return ws

    def cleanup(self) -> None:
        now = time.time()
        for p in self.directory.glob("*"):
            with self._lock:  # see get()
                try:
                    if now - p.stat().st_mtime <= self.ttl:
                        continue
                    mem = self._mem.get(p.name.split(".", 1)[0])
                    if mem is not None and now - mem["used"] <= self.ttl:
                        continue  # in use, though its file's time could not be updated
                    p.unlink(missing_ok=True)
                except OSError:
                    continue
        with self._lock:
            for wid in [w for w, ws in self._mem.items() if now - ws["used"] > self.ttl]:
                self._mem.pop(wid, None)
