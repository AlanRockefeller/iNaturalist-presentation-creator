"""Workspaces expire a day after they were last used."""

import os
import time

import pytest

from app.workspace import WorkspaceExpired, WorkspaceStore


def test_each_use_extends_a_workspace(tmp_path, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr("app.workspace.time.time", lambda: now[0])
    store = WorkspaceStore(tmp_path, ttl_seconds=100)
    wid = store.create({1: {"id": 1}}, {"a": [1]})
    for _ in range(3):  # 240 s after creation, but never 100 s without use
        now[0] += 80
        assert store.get(wid)["observations"][1] == {"id": 1}
    now[0] += 101
    with pytest.raises(WorkspaceExpired):
        store.get(wid)


def test_last_use_survives_a_restart(tmp_path):
    store = WorkspaceStore(tmp_path, ttl_seconds=100)
    wid = store.create({1: {"id": 1}}, {})
    path = tmp_path / f"{wid}.json.gz"
    fresh = WorkspaceStore(tmp_path, ttl_seconds=100)  # nothing in memory
    assert fresh.get(wid)["observations"]
    old = time.time() - 150
    os.utime(path, (old, old))
    with pytest.raises(WorkspaceExpired):
        WorkspaceStore(tmp_path, ttl_seconds=100).get(wid)


def test_a_workspace_whose_file_is_gone_has_expired(tmp_path):
    store = WorkspaceStore(tmp_path, ttl_seconds=100)
    wid = store.create({1: {"id": 1}}, {})
    (tmp_path / f"{wid}.json.gz").unlink()
    with pytest.raises(WorkspaceExpired):
        store.get(wid)  # it was still in memory
    with pytest.raises(WorkspaceExpired):
        store.get(wid)
