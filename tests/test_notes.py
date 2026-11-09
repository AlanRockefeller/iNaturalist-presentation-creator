"""Presenter notes: what is sent to Claude, the CLI wrapper, access and jobs.

Claude is never called: the API tests use FakeNotesWriter, and the CLI tests
run a small fake ``claude`` script.
"""

import json
import os
import signal
import stat
import sys
import threading
import time

import pytest

from app.models import ObservationState
from app.notes import (
    MAX_BULLETS, AccessGuard, ClaudeCliWriter, NotesCancelled, NotesError, build_prompt, clean_notes, subject_for,
)
from app.project import add_observations, load_project, project_to_json, refresh_project
from tests.conftest import make_obs, wait_job
from tests.helpers import norm, project, source
from tests.test_api import URL_A, URL_B, _load, _new_project, _seed, _source

CODE = {"X-Notes-Code": "letmein"}


# ---------------------------------------------------------------- what is sent

def test_subject_has_names_and_ranks_only():
    obs = norm(make_obs(1, 47602, user="alice", name="Alice Smith", faves=17, observed_on="2026-11-14",
                        place="Point Reyes, CA, US"))[1]
    subject = subject_for(obs, None)
    assert subject == {"name": "Amanita muscaria", "rank": "species", "common_name": "Fly Agaric",
                       "kingdom": "Fungi", "phylum": "Basidiomycota", "class": "Agaricomycetes",
                       "order": "Agaricales", "family": "Amanitaceae", "genus": "Amanita"}
    prompt = build_prompt({"n0": subject})
    for private in ("17", "alice", "Alice", "Point Reyes", "2026", "November"):
        assert private not in prompt


def test_subject_uses_the_name_on_the_slide():
    obs = norm(make_obs(1, 47701))[1]  # Psilocybe sp.
    st = ObservationState(id=1, overrides={"scientific": "Psilocybe alimapensis nom. prov."})
    subject = subject_for(obs, st)
    assert subject["name"] == "Psilocybe alimapensis nom. prov."
    assert subject["inaturalist_name"] == "Psilocybe sp." and subject["rank"] == "genus"


def test_clean_notes():
    text = clean_notes([
        "• First point — with a dash",
        "  - Second\npoint\x00here  ",
        42,
        "",
        "x" * 400,
        "Four", "Five", "Six", "Seven", "Eight", "Nine",
    ])
    lines = text.split("\n")
    assert len(lines) == MAX_BULLETS
    assert lines[0] == "• First point, with a dash"
    assert lines[1] == "• Second point here"
    assert lines[2].startswith("• xxx") and lines[2].endswith("…") and len(lines[2]) == 2 + 300
    assert lines[3:] == ["• Four", "• Five", "• Six", "• Seven", "• Eight"]
    assert clean_notes(None) == "" and clean_notes(["   "]) == ""


# ---------------------------------------------------------------- the project file

def test_notes_are_validated_saved_and_survive_refresh():
    st = ObservationState(id=1, notes="• One\r\n• Two\x00\x0b", notes_name="Amanita muscaria")
    assert st.notes == "• One\n• Two  "
    with pytest.raises(ValueError):
        ObservationState(id=1, notes="x" * 4001)

    obs = norm(make_obs(1, 47602), make_obs(2, 47692))
    pr = add_observations(project(source("a", "alice")), [1, 2], obs, {1: ["a"], 2: ["a"]}, placement="append")
    pr = pr.model_copy(update={"observations": [pr.observations[0].model_copy(update={
        "notes": "• About it", "notes_name": "Amanita muscaria"}), pr.observations[1]]})
    data = project_to_json(pr)
    assert data["observations"][0]["notes"] == "• About it"
    assert "notes" not in data["observations"][1]  # no notes: left out of the file
    again = load_project(json.loads(json.dumps(data)))
    assert again.observations[0].notes == "• About it"

    refreshed, _ = refresh_project(again, obs, {"a": [1, 2]})
    assert refreshed.observations[0].notes == "• About it"
    assert refreshed.observations[0].notes_name == "Amanita muscaria"


# ---------------------------------------------------------------- access code

def test_access_guard_locks_after_repeated_wrong_codes():
    now = [0.0]
    guard = AccessGuard("secret", max_failures=3, window=60, clock=lambda: now[0])
    assert guard.check("a", None) == "missing"
    assert guard.check("a", "secret") == "ok"
    assert [guard.check("a", "nope") for _ in range(3)] == ["denied"] * 3
    assert guard.check("a", "secret") == "locked"
    assert guard.check("b", "secret") == "ok"  # other clients are not affected
    now[0] = 61
    assert guard.check("a", "secret") == "ok"
    assert AccessGuard("").check("a", "anything") == "denied"
    long_code = "x" * 200
    assert AccessGuard(long_code).check("a", long_code + "y") == "denied"
    assert AccessGuard("y" * 250).check("a", "y" * 250) == "ok"


def test_notes_are_off_without_a_writer_and_code(app_client):
    assert 'data-notes-enabled="0"' in app_client.get("/").text
    r = app_client.post("/api/notes", json={"project": {}, "workspace_id": "x"}, headers=CODE)
    assert r.status_code == 503


def test_notes_need_the_access_code(notes_client):
    tc = notes_client
    assert 'data-notes-enabled="1"' in tc.get("/").text
    body = {"project": {"format": "dikarya-presentation", "schema_version": 5}, "workspace_id": "x"}
    missing = tc.post("/api/notes", json=body)
    assert missing.status_code == 403 and "access code is needed" in missing.json()["detail"]
    for _ in range(10):
        assert tc.post("/api/notes", json=body, headers={"X-Notes-Code": "wrong"}).status_code == 403
    assert tc.post("/api/notes", json=body, headers=CODE).status_code == 429  # locked for an hour


# ---------------------------------------------------------------- jobs

def _loaded(tc, fake):
    _seed(fake)
    fake.add(make_obs(5, 47602, user="dave", photos=((51, 1000, 1000),)))  # a second Fly Agaric
    return _load(tc, _new_project(tc, _source(tc, URL_A, "a"), _source(tc, URL_B, "b")))


def _notes(tc, result, ids=None, replace=False, web_search=False):
    r = tc.post("/api/notes", headers=CODE, json={
        "project": result["project"], "workspace_id": result["workspace_id"],
        "observation_ids": ids, "replace": replace, "web_search": web_search})
    assert r.status_code == 200, r.text
    return wait_job(tc, r.json()["id"])


def test_notes_for_every_slide_once_per_name(notes_client, fake, notes_writer):
    tc = notes_client
    result = _loaded(tc, fake)
    job = _notes(tc, result)
    assert job["state"] == "done", job
    notes = job["result"]["notes"]
    assert set(notes) == {"1", "2", "3", "4", "5"}
    assert notes["2"] == notes["5"] == {"text": "• About Amanita muscaria", "name": "Amanita muscaria"}
    asked = [s["name"] for call in notes_writer.calls for s in call.values()]
    assert sorted(asked) == sorted(["Hygrocybe conica", "Amanita muscaria", "Psilocybe cyanescens", "Hygrocybe sp."])
    assert len(asked) == len(set(asked))
    prompts = json.dumps(notes_writer.calls)
    assert "dave" not in prompts and "faves" not in prompts


def test_existing_notes_are_kept_unless_replaced(notes_client, fake, notes_writer):
    tc = notes_client
    result = _loaded(tc, fake)
    for st in result["project"]["observations"]:
        if st["id"] in (1, 2):
            st["notes"] = "• Typed by hand"
    job = _notes(tc, result)
    assert set(job["result"]["notes"]) == {"3", "4", "5"} and job["result"]["skipped"] == 2

    notes_writer.calls.clear()
    one = _notes(tc, result, ids=[1], replace=True)
    assert set(one["result"]["notes"]) == {"1"} and len(notes_writer.calls) == 1

    for st in result["project"]["observations"]:
        st["notes"] = "• Typed by hand"
    r = tc.post("/api/notes", headers=CODE, json={"project": result["project"], "workspace_id": result["workspace_id"]})
    assert r.status_code == 429 and "already has notes" in r.json()["detail"]


def test_daily_limit_keeps_what_was_written(notes_client, fake, settings):
    tc = notes_client
    result = _loaded(tc, fake)
    settings.notes_batch_size = 1
    settings.notes_concurrency = 1
    tc.app.state.services.notes_budget.limit = 2
    job = _notes(tc, result)
    assert job["state"] == "error" and "daily allowance of presenter notes" in job["error"]
    assert len({v["name"] for v in job["result"]["notes"].values()}) == 2


def test_daily_limit_lets_a_paid_call_finish(notes_client, fake, notes_writer, settings):
    """One call is paid for and still writing when the other finds the
    allowance used up: the first one's notes are kept."""
    tc = notes_client
    result = _loaded(tc, fake)  # 4 names
    settings.notes_batch_size = 2
    settings.notes_concurrency = 2
    tc.app.state.services.notes_budget.limit = 2
    notes_writer.block = threading.Event()
    r = tc.post("/api/notes", headers=CODE, json={"project": result["project"], "workspace_id": result["workspace_id"]})
    job_id = r.json()["id"]
    deadline = time.time() + 5
    while tc.get(f"/api/jobs/{job_id}").json()["done"] < 2 and time.time() < deadline:
        time.sleep(0.02)  # the unpaid batch has been counted while the paid one writes
    assert len(notes_writer.calls) == 1
    notes_writer.block.set()
    job = wait_job(tc, job_id)
    assert job["state"] == "error" and "daily allowance of presenter notes" in job["error"]
    assert len({v["name"] for v in job["result"]["notes"].values()}) == 2
    assert job["result"]["failed"] == []


def test_daily_limit_pays_for_part_of_a_batch(notes_client, fake, notes_writer, settings):
    tc = notes_client
    result = _loaded(tc, fake)  # 4 names in one batch, 3 left in the allowance
    tc.app.state.services.notes_budget.limit = 3
    job = _notes(tc, result)
    assert job["state"] == "error" and len(notes_writer.calls[0]) == 3
    assert len({v["name"] for v in job["result"]["notes"].values()}) == 3


def test_failed_names_are_reported_and_fatal_errors_stop(notes_client, fake, notes_writer):
    tc = notes_client
    result = _loaded(tc, fake)
    notes_writer.fail_names = {"Psilocybe cyanescens"}
    job = _notes(tc, result)
    assert job["state"] == "done" and job["result"]["failed"] == ["Psilocybe cyanescens"]
    assert "3" not in job["result"]["notes"] and "Psilocybe cyanescens" in job["warnings"][0]

    notes_writer.error = NotesError("Claude's usage limit has been reached. Please try again later.", fatal=True)
    job = _notes(tc, result, replace=True)
    assert job["state"] == "error" and "usage limit" in job["error"]


def test_cancel_stops_writing(notes_client, fake, notes_writer):
    tc = notes_client
    result = _loaded(tc, fake)
    notes_writer.block = threading.Event()
    r = tc.post("/api/notes", headers=CODE, json={"project": result["project"], "workspace_id": result["workspace_id"]})
    job_id = r.json()["id"]
    deadline = time.time() + 5
    while not notes_writer.calls and time.time() < deadline:
        time.sleep(0.02)
    assert tc.delete(f"/api/jobs/{job_id}").status_code == 200
    job = wait_job(tc, job_id)
    assert job["state"] == "cancelled" and job["result"]["notes"] == {}
    assert tc.get("/healthz").json()["jobs"]["notes"] == 0


def test_notes_finished_after_stop_are_not_kept(notes_client, fake, notes_writer):
    tc = notes_client
    result = _loaded(tc, fake)
    notes_writer.block = threading.Event()
    notes_writer.ignore_cancel = True  # Claude finishes its answer after all
    r = tc.post("/api/notes", headers=CODE, json={
        "project": result["project"], "workspace_id": result["workspace_id"], "observation_ids": [1], "replace": True})
    job_id = r.json()["id"]
    deadline = time.time() + 5
    while not notes_writer.calls and time.time() < deadline:
        time.sleep(0.02)
    assert tc.delete(f"/api/jobs/{job_id}").status_code == 200
    notes_writer.block.set()
    job = wait_job(tc, job_id)
    assert job["state"] == "cancelled" and job["result"]["notes"] == {}


def test_selected_observations_only(notes_client, fake, notes_writer):
    tc = notes_client
    result = _loaded(tc, fake)
    job = _notes(tc, result, ids=[1, 3])
    assert job["state"] == "done" and set(job["result"]["notes"]) == {"1", "3"}


def test_web_search_uses_smaller_batches_and_can_be_off(notes_client, fake, notes_writer, settings):
    tc = notes_client
    result = _loaded(tc, fake)  # 4 names
    settings.notes_web_batch_size = 2
    assert 'data-notes-web="1"' in tc.get("/").text
    job = _notes(tc, result, web_search=True)
    assert job["state"] == "done" and notes_writer.web_search == [True, True]
    assert sorted(len(c) for c in notes_writer.calls) == [2, 2]

    settings.notes_web_search = False
    r = tc.post("/api/notes", headers=CODE, json={
        "project": result["project"], "workspace_id": result["workspace_id"], "web_search": True, "replace": True})
    assert r.status_code == 422 and "turned off" in r.json()["detail"]
    assert 'data-notes-web="0"' in tc.get("/").text


# ---------------------------------------------------------------- the CLI wrapper

FAKE_CLI = r'''#!{python}
import json, os, sys, time
home = os.environ["HOME"]
mode = open(os.path.join(home, "mode")).read().strip()
json.dump({{"argv": sys.argv[1:], "env": sorted(os.environ), "cwd": os.getcwd()}},
          open(os.path.join(home, "called.json"), "w"))
prompt = sys.stdin.read()
items = json.loads(prompt[prompt.index("["):])
notes = [{{"key": i["key"], "bullets": ["About " + i["name"], "Second — point"]}} for i in items]
if mode == "ok":
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                      "structured_output": {{"notes": notes}}, "total_cost_usd": 0.01}}))
elif mode == "text":
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                      "result": json.dumps({{"notes": notes[:1]}})}}))
elif mode == "auth":
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                      "result": "OAuth token has expired", "api_error_status": 401}}))
elif mode == "limit":
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                      "result": "Claude AI usage limit reached|1700000000"}}))
elif mode == "garbage":
    print("not json")
    sys.exit(1)
elif mode == "sleep":
    import subprocess
    child = subprocess.Popen(["/bin/sleep", "30"])  # something the CLI started
    open(os.path.join(home, "pids"), "w").write(f"{{os.getpid()}} {{child.pid}}")
    time.sleep(30)
'''


@pytest.fixture
def cli(settings, tmp_path):
    script = tmp_path / "claude"
    script.write_text(FAKE_CLI.format(python=sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    settings.notes_cli = str(script)
    settings.notes_token = "tok-123"
    settings.notes_access_code = "letmein"
    settings.claude_dir.mkdir(parents=True, exist_ok=True)

    def run(mode, subjects=None, cancelled=lambda: False, web_search=False):
        (settings.claude_dir / "mode").write_text(mode)
        subjects = subjects or {"n0": {"name": "Amanita muscaria"}, "n1": {"name": "Hygrocybe conica"}}
        return ClaudeCliWriter(settings).write_batch(subjects, cancelled, web_search=web_search)

    run.called = lambda: json.loads((settings.claude_dir / "called.json").read_text())
    return run


def test_cli_runs_without_tools_or_inherited_environment(cli, settings, monkeypatch):
    monkeypatch.setenv("SOME_SERVER_SECRET", "x")
    assert settings.notes_configured()
    written = cli("ok")
    assert written == {"n0": "• About Amanita muscaria\n• Second, point",
                       "n1": "• About Hygrocybe conica\n• Second, point"}
    called = cli.called()
    argv = called["argv"]
    assert argv[0] == "-p" and argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert "SOME_SERVER_SECRET" not in called["env"] and "CLAUDE_CODE_OAUTH_TOKEN" in called["env"]
    assert called["cwd"] == str(settings.claude_dir / "work")
    assert "--allowedTools" not in argv and "You can search the web" not in argv[argv.index("--system-prompt") + 1]


def test_cli_with_web_search_allows_only_websearch(cli, settings):
    cli("ok", web_search=True)
    argv = cli.called()["argv"]
    assert argv[argv.index("--tools") + 1] == "WebSearch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch"
    assert "WebFetch" not in " ".join(argv[:argv.index("--system-prompt")])
    assert "You can search the web" in argv[argv.index("--system-prompt") + 1]


def test_cli_answer_as_text_and_missing_keys(cli):
    assert set(cli("text")) == {"n0"}  # n1 left out: reported as failed by the job


@pytest.mark.parametrize("mode, message, fatal", [
    ("auth", "could not sign in", True),
    ("limit", "usage limit", True),
    ("garbage", "could not write", False),
])
def test_cli_errors(cli, mode, message, fatal):
    with pytest.raises(NotesError, match=message) as info:
        cli(mode)
    assert info.value.fatal is fatal


def _running(pid: int) -> bool:
    """A process that exists and is not a zombie waiting to be reaped."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False


def _assert_all_stopped(settings):
    pids = [int(p) for p in (settings.claude_dir / "pids").read_text().split()]
    try:
        deadline = time.monotonic() + 5
        while any(_running(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not any(_running(p) for p in pids), "the CLI or a process it started is still running"
    finally:
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def test_cli_timeout_and_cancel_kill_the_process(cli, settings):
    settings.notes_call_timeout = 1.5
    started = time.monotonic()
    with pytest.raises(NotesError, match="too long"):
        cli("sleep")
    assert time.monotonic() - started < 6
    _assert_all_stopped(settings)  # the CLI and the process it started

    started = time.monotonic()
    with pytest.raises(NotesCancelled):
        cli("sleep", cancelled=lambda: time.monotonic() - started > 0.5)
    assert time.monotonic() - started < 5
    _assert_all_stopped(settings)


def test_cli_missing_is_a_clear_error(cli, settings):
    settings.notes_cli = str(settings.work_dir / "no-such-claude")
    assert not settings.notes_configured()
    with pytest.raises(NotesError, match="not installed") as info:
        ClaudeCliWriter(settings).write_batch({"n0": {"name": "x"}}, lambda: False)
    assert info.value.fatal


def test_v6_file_migrates_with_no_notes():
    data = {"format": "dikarya-presentation", "schema_version": 6,
            "observations": [{"id": 1, "selected_photo_ids": [11]}], "order": [1]}
    pr = load_project(data)
    assert pr.schema_version == 7 and pr.observations[0].notes is None
    assert pr.settings.presenter_notes is True
    assert project_to_json(pr)["schema_version"] == 7
