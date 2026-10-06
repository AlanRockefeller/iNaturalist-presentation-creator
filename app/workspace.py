"""Temporary server-side store of normalized iNaturalist metadata.

A workspace holds what the last load fetched (observations + per-source result
lists) so sorting, preview and generation work from trusted, server-derived data
without the browser re-uploading megabytes of metadata. Workspaces are gzipped
JSON files that expire after ``workspace_ttl_seconds``; they hold no user edits
and no photographs.
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
        self._remember(wid, self._inflate(data))
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
        path = self._path(wid)
        with self._lock:
            ws = self._mem.get(wid)
            if ws is not None:
                self._mem.move_to_end(wid)
        if ws is None:
            try:
                with gzip.open(path, "rb") as fh:
                    ws = self._inflate(json.loads(fh.read()))
            except (OSError, ValueError, EOFError):
                raise WorkspaceExpired() from None
            self._remember(wid, ws)
        if time.time() - ws["created"] > self.ttl:
            raise WorkspaceExpired()
        try:
            os.utime(path)
        except OSError:
            pass
        return ws

    def cleanup(self) -> None:
        now = time.time()
        for p in self.directory.glob("*"):
            try:
                if now - p.stat().st_mtime > self.ttl:
                    p.unlink(missing_ok=True)
            except OSError:
                continue
        with self._lock:
            for wid in [w for w, ws in self._mem.items() if now - ws["created"] > self.ttl]:
                self._mem.pop(wid, None)
