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


def test_a_use_whose_file_time_cannot_be_set_still_counts(tmp_path, monkeypatch, caplog):
    import logging

    store = WorkspaceStore(tmp_path, ttl_seconds=100)
    wid = store.create({1: {"id": 1}}, {})
    path = tmp_path / f"{wid}.json.gz"
    old = time.time() - 150
    os.utime(path, (old, old))  # its file looks unused for longer than the TTL

    def refuse(*args, **kwargs):
        raise PermissionError()

    monkeypatch.setattr("app.workspace.os.utime", refuse)
    caplog.set_level(logging.WARNING)
    assert store.get(wid)["observations"]
    assert any("event=workspace.touch_failed" in r.message for r in caplog.records)
    store.cleanup()
    assert path.exists()  # in use, so cleanup keeps it


def test_two_uses_at_once_never_put_the_time_back(tmp_path):
    store = WorkspaceStore(tmp_path, ttl_seconds=100)
    wid = store.create({}, {})
    later = time.time() + 50
    store._mem[wid]["used"] = later  # a use that finished after this one began
    assert store.get(wid)["used"] == later


def test_a_copy_loaded_meanwhile_does_not_replace_a_renewed_one(tmp_path, monkeypatch):
    wid = WorkspaceStore(tmp_path, ttl_seconds=100).create({1: {"id": 1}}, {})
    fresh = WorkspaceStore(tmp_path, ttl_seconds=100)  # nothing in memory: get() reads the file
    newer = {"created": 0, "observations": {}, "source_results": {}, "used": time.time() + 30}
    real = WorkspaceStore._inflate

    def read_while_another_request_publishes(data):
        fresh._mem[wid] = newer  # loaded and renewed by another request meanwhile
        return real(data)

    monkeypatch.setattr(fresh, "_inflate", read_while_another_request_publishes)
    assert fresh.get(wid) is newer and fresh._mem[wid] is newer
