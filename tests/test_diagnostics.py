"""Logging: one useful line per request or job, correlated, and nothing noisy."""

import logging
import re
import time

from fastapi.testclient import TestClient

from app.diagnostics import AlreadyLoggedFilter, client_bucket, fmt_value
from app.main import create_app
from tests.conftest import make_obs, wait_job
from tests.helpers import project, source


def _lines(caplog, event):
    return [r for r in caplog.records if r.message.startswith(f"event={event} ")]


def test_generated_ids_ignore_incoming_headers_and_failures_say_what_was_asked(app_client, caplog):
    caplog.set_level(logging.WARNING)
    supplied = "b" * 32
    first = app_client.get("/not-a-route?token=probe", headers={"X-Request-ID": supplied, "User-Agent": "scanner/1.0"})
    second = app_client.get("/healthz", headers={"X-Request-ID": supplied})
    assert re.fullmatch(r"[0-9a-f]{32}", first.headers["x-request-id"])
    assert first.headers["x-request-id"] != supplied
    assert second.headers["x-request-id"] not in (supplied, first.headers["x-request-id"])
    failed = _lines(caplog, "request.failed")
    assert len(failed) == 1
    line = failed[0]
    assert line.request_id == first.headers["x-request-id"]
    assert "route=/not-a-route" in line.message and "status=404" in line.message
    # For spotting probes: what was asked for, and by what.
    assert 'query="token=probe"' in line.message and "ua=scanner/1.0" in line.message
    assert 'detail="Not Found"' in line.message


def test_successful_polls_health_and_assets_are_not_logged(app_client, caplog):
    caplog.set_level(logging.INFO)
    app_client.get("/healthz")
    app_client.get("/static/js/theme.js")
    app_client.get("/robots.txt")
    job = app_client.post("/api/load", json={"project": project().model_dump(mode="json")}).json()
    wait_job(app_client, job["id"])
    requests = _lines(caplog, "request")
    assert [re.search(r"route=(\S+)", r.message).group(1) for r in requests] == ["/api/load"]
    assert f"job_id={job['id']}" in requests[0].message


def test_api_lines_carry_what_the_request_did(app_client, caplog):
    caplog.set_level(logging.INFO)
    url = "https://www.inaturalist.org/observations?taxon_id=47170&place_id=1"
    assert app_client.post("/api/sources/parse", json={"url": url, "username": ""}).status_code == 200
    app_client.post("/api/sources/parse", json={"url": "https://evil.example/observations?x=1", "username": ""})
    ok, bad = _lines(caplog, "request") + _lines(caplog, "request.failed")
    assert ok.levelno == logging.INFO and "type=url" in ok.message and "source=" in ok.message
    assert bad.levelno == logging.WARNING and 'input="https://evil.example/observations?x=1"' in bad.message
    assert "detail=" in bad.message and "status=422" in bad.message


def test_submitted_text_cannot_forge_log_lines():
    assert fmt_value("ok") == "ok"
    assert fmt_value("two words") == '"two words"'
    forged = fmt_value("x\nevent=request.failed status=500")
    assert "\n" not in forged and forged.startswith('"')
    assert fmt_value("a" * 400) == "a" * 300 + "..."


def test_body_limit_rejections_are_logged_for_declared_and_streamed_bodies(app_client, settings, caplog):
    declared = app_client.post("/api/project/validate", content=b"x" * (settings.max_body_bytes + 1))

    def chunks():
        for _ in range(settings.max_body_bytes // 65536 + 2):
            yield b"x" * 65536

    streamed = app_client.post("/api/load", content=chunks(), headers={"content-type": "application/json"})
    assert declared.status_code == 413 and streamed.status_code == 413
    failed = _lines(caplog, "request.failed")
    assert {r.request_id for r in failed} == {declared.headers["x-request-id"], streamed.headers["x-request-id"]}
    assert all("status=413" in r.message for r in failed)


def test_a_crash_is_logged_once_with_its_traceback(settings, services, caplog):
    app = create_app(settings, services)

    @app.get("/synthetic-failure")
    async def fail():
        raise RuntimeError("synthetic failure")

    with TestClient(app, raise_server_exceptions=False, client=("203.0.113.8", 50000)) as tc:
        response = tc.get("/synthetic-failure")
    assert response.status_code == 500
    lines = _lines(caplog, "request.failed")
    assert len(lines) == 1
    line = lines[0]
    assert line.request_id == response.headers["x-request-id"] and line.client_ip == "203.0.113.8"
    assert "status=500" in line.message and "error_type=RuntimeError" in line.message
    assert line.exc_info and line.exc_info[1].args == ("synthetic failure",)
    # Uvicorn's own "Exception in ASGI application" copy of it is dropped.
    duplicate = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application",
                                  None, line.exc_info)
    assert not AlreadyLoggedFilter().filter(duplicate)
    other = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "x", None, None)
    assert AlreadyLoggedFilter().filter(other)


def test_httpx_request_lines_are_off():
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert not logging.getLogger("uvicorn.access").filter(
        logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "GET / 200", None, None))


def _finished_records(caplog, job_id):
    # The public terminal state can become visible just before the final log.
    until = time.monotonic() + 2
    while time.monotonic() < until:
        rows = [r for r in caplog.records if getattr(r, "job_id", None) == job_id]
        if any(r.message.startswith("event=job.finished") for r in rows):
            return rows
        time.sleep(0.01)
    raise AssertionError("job had no terminal diagnostic event")


def test_job_lines_keep_the_submitter_and_say_why_it_failed(settings, services, caplog):
    caplog.set_level(logging.INFO)
    with TestClient(create_app(settings, services), client=("203.0.113.9", 50000)) as tc:
        response = tc.post("/api/load", json={"project": project().model_dump(mode="json")})
        job_id = response.json()["id"]
        final = wait_job(tc, job_id)
        poll = tc.get(f"/api/jobs/{job_id}")
    assert final["state"] == "error"
    rows = _finished_records(caplog, job_id)
    assert {r.request_id for r in rows} == {response.headers["x-request-id"]}
    assert {r.client_ip for r in rows} == {"203.0.113.9"}
    assert poll.headers["x-request-id"] != response.headers["x-request-id"]
    assert [r.message.split()[0] for r in rows] == ["event=job.started", "event=job.finished"]
    finished = rows[-1]
    assert finished.levelno == logging.WARNING
    assert "state=error" in finished.message and "error_type=JobFailed" in finished.message
    assert 'error="Add at least one source first."' in finished.message
    assert "log_context" not in final


def test_finished_load_reports_what_it_loaded(settings, services, fake, caplog):
    caplog.set_level(logging.INFO)
    fake.add(make_obs(1, 47692, user="alice"), make_obs(2, 47602, user="alice"))
    body = {"project": project(source("a", "alice")).model_dump(mode="json")}
    with TestClient(create_app(settings, services)) as tc:
        job_id = tc.post("/api/load", json=body).json()["id"]
        assert wait_job(tc, job_id)["state"] == "done"
    rows = _finished_records(caplog, job_id)
    per_source = [r for r in rows if r.message.startswith("event=load.source")]
    assert len(per_source) == 1 and "results=2" in per_source[0].message and "user_id=alice" in per_source[0].message
    finished = rows[-1].message
    assert "state=done" in finished and "observations=2" in finished and "first_load=true" in finished
    assert re.search(r"api_requests=\d+", finished) and re.search(r"api_left=\d+", finished)


def test_inat_retry_and_reused_worker_keep_correct_job_context(settings, services, fake, caplog):
    caplog.set_level(logging.INFO)
    fake.add(make_obs(1, 47692, user="alice"))
    fake.fail_429_times = 1
    body = {"project": project(source("a", "alice")).model_dump(mode="json")}
    with TestClient(create_app(settings, services), client=("203.0.113.10", 50000)) as tc:
        first = tc.post("/api/load", json=body)
        first_id = first.json()["id"]
        assert wait_job(tc, first_id)["state"] == "done"
        second = tc.post("/api/load", json=body)
        second_id = second.json()["id"]
        assert wait_job(tc, second_id)["state"] == "done"
    first_rows = _finished_records(caplog, first_id)
    second_rows = _finished_records(caplog, second_id)
    assert {r.request_id for r in first_rows} == {first.headers["x-request-id"]}
    assert {r.request_id for r in second_rows} == {second.headers["x-request-id"]}
    retries = [r for r in first_rows if "event=inat.retry" in r.message]
    assert len(retries) == 1 and "status=429" in retries[0].message
    assert all(r.client_ip == "203.0.113.10" for r in first_rows + second_rows)


def test_ipv6_clients_are_limited_per_64():
    assert client_bucket("203.0.113.5") == "203.0.113.5"
    assert client_bucket("2001:db8:1:2:aaaa::1") == client_bucket("2001:db8:1:2:bbbb::9") == "2001:db8:1:2::/64"
    assert client_bucket("::ffff:203.0.113.5") == "203.0.113.5"
    assert client_bucket("unknown") == "unknown"


# ---------------------------------------------------------------- review fixes (PR 3)

def test_no_unprintable_character_can_start_a_new_log_line():
    for char in (" ", " ", "\x85", "‮", "\x9b"):
        out = fmt_value(f"a{char}b")
        assert char not in out and len(out.splitlines()) == 1 and out.startswith('"')


def test_percent_signs_in_context_values_are_logged_as_they_are(caplog):
    from app.diagnostics import bind_context, get_logger

    caplog.set_level(logging.INFO)
    with bind_context({"request_id": "r1", "client_ip": "fe80::1%eth0"}):
        get_logger("tests").info("event=x a=%s", 5)
    assert caplog.records[-1].message == "event=x a=5 request_id=r1 client_ip=fe80::1%eth0"


def _asgi(app, scope_path="/api/x", messages=({"type": "http.request", "body": b"", "more_body": False},),
          headers=()):
    import asyncio

    sent, queue = [], list(messages)

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": scope_path, "query_string": b"",
             "headers": list(headers), "client": ("203.0.113.1", 1)}
    try:
        asyncio.run(app(scope, receive, send))
    except BaseException as exc:  # noqa: BLE001 - the tests look at what was raised
        return sent, exc
    return sent, None


def test_body_limit_adds_up_separate_streamed_messages():
    from app.main import BodyLimitMiddleware

    reached = []

    async def inner(scope, receive, send):
        reached.append(True)

    chunks = [{"type": "http.request", "body": b"x" * 60, "more_body": more} for more in (True, True, False)]
    sent, _ = _asgi(BodyLimitMiddleware(inner, limit=100), messages=chunks, headers=[(b"transfer-encoding", b"chunked")])
    assert sent[0]["status"] == 413 and not reached  # 60 + 60 > 100, though each message is under it
    small = chunks[:1] + [{"type": "http.request", "body": b"x" * 30, "more_body": False}]
    sent, _ = _asgi(BodyLimitMiddleware(inner, limit=100), messages=small, headers=[(b"transfer-encoding", b"chunked")])
    assert reached and not sent  # 90 bytes went through to the app


def test_cancelled_requests_are_logged_even_on_quiet_routes(caplog):
    import asyncio

    from app.diagnostics import RequestDiagnosticsMiddleware

    async def cut_off(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise asyncio.CancelledError()

    caplog.set_level(logging.INFO)
    _, exc = _asgi(RequestDiagnosticsMiddleware(cut_off), scope_path="/healthz")
    assert isinstance(exc, asyncio.CancelledError)
    line = _lines(caplog, "request")[-1]
    assert "route=/healthz" in line.message and "cancelled=true" in line.message and line.levelno == logging.INFO


def test_an_error_body_cut_at_the_capture_limit_still_logs_its_text(caplog):
    from app.diagnostics import RequestDiagnosticsMiddleware

    async def big_error(scope, receive, send):
        await send({"type": "http.response.start", "status": 422,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": ('{"detail": "' + "x" * 3000 + '"}').encode()})

    _asgi(RequestDiagnosticsMiddleware(big_error))
    line = _lines(caplog, "request.failed")[-1].message
    assert 'detail="{\\"detail\\": \\"xxx' in line
