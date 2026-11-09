"""Logging for fixing bugs, tracking usage and spotting attacks.

Each line should serve one of those, and nothing more:

* one line per request (``event=request``) with its route, status, time and
  what it did (fields added by the endpoint with ``annotate``); failures
  (``event=request.failed``) also get the query string, the error message the
  user saw and the browser's User-Agent;
* successful static files, health checks and job-status polls are not logged
  (the browser polls about once a second), nor are httpx's per-request lines;
* one ``job.started`` and one ``job.finished`` line per background job, with
  its results (see jobs.py);
* a crash is logged once, with its traceback.

Values are quoted when they contain spaces, quotes or anything unprintable,
and every unprintable character (newlines, U+2028 and U+2029, C1 controls,
bidirectional overrides) is escaped, so a value is always one line and
submitted text cannot fake extra log lines or fields.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from starlette.requests import ClientDisconnect

_context: ContextVar[dict] = ContextVar("presentations_diagnostics", default={})
log = logging.getLogger("presentations.requests")

CONTEXT_KEYS = ("request_id", "client_ip", "job_id")
MAX_VALUE = 300
# Successful requests not worth a line: assets, health checks, robots, and the
# browser polling a job (the job logs its own start and finish).
QUIET_PREFIXES = ("/static/",)
QUIET_PATHS = {"/healthz", "/robots.txt", "/favicon.ico"}
QUIET_ROUTES = {("GET", "/api/jobs/{job_id}")}
LOGGED_MARK = "_presentations_logged"


def snapshot() -> dict[str, str]:
    """The current request or job's ids, for handing to a background job."""
    context = _context.get()
    return {key: context[key] for key in CONTEXT_KEYS if key in context}


@contextmanager
def bind_context(context: dict):
    """Use ``context`` for log lines in this block. It is not copied, so
    ``annotate`` calls inside (also from worker threads) reach its fields."""
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


def annotate(**fields: Any) -> None:
    """Add details to the current request's log line. None values are skipped."""
    target = _context.get().get("fields")
    if target is not None:
        target.update({key: value for key, value in fields.items() if value is not None})


def fmt_value(value: Any) -> str:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, float):
        text = f"{value:.1f}"
    elif isinstance(value, (list, tuple, set)):
        text = ",".join(str(v) for v in value)
    else:
        text = str(value)
    if len(text) > MAX_VALUE:
        text = text[:MAX_VALUE] + "..."
    if not text or not text.isprintable() or any(c in text for c in ' "=\\'):
        # json.dumps quotes it and escapes C0 controls; escape the other
        # unprintable characters too, which it leaves as they are.
        text = "".join(c if c.isprintable() else _escape(c) for c in json.dumps(text, ensure_ascii=False))
    return text


def _escape(char: str) -> str:
    code = ord(char)
    return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"


def fields_text(fields: dict[str, Any]) -> str:
    return " ".join(f"{key}={fmt_value(value)}" for key, value in fields.items())


def log_event(logger: logging.LoggerAdapter | logging.Logger, level: int, event: str,
              exc_info: BaseException | None = None, **fields: Any) -> None:
    """Log ``event=<event>`` followed by ``key=value`` fields."""
    message = f"event={event}" + (f" {fields_text(fields)}" if fields else "")
    logger.log(level, "%s", message, exc_info=exc_info)
    if exc_info is not None:
        mark_logged(exc_info)


def mark_logged(exc: BaseException) -> None:
    """Note that ``exc`` has been logged with its traceback, so Uvicorn's own
    copy of it is dropped (see AlreadyLoggedFilter)."""
    try:
        setattr(exc, LOGGED_MARK, True)
    except (AttributeError, TypeError):
        pass


class AlreadyLoggedFilter(logging.Filter):
    """For uvicorn.error: drop "Exception in ASGI application" when the
    request middleware has already logged that exception with its context."""

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not getattr(exc, LOGGED_MARK, False)


def client_ip(scope) -> str:
    """The client's address. Behind nginx, Uvicorn (--proxy-headers, trusting
    only 127.0.0.1) has already put the real client in scope["client"]."""
    peer = scope.get("client")
    try:
        return str(ipaddress.ip_address(peer[0])) if peer else "unknown"
    except ValueError:
        return "unknown"


def client_bucket(ip: str) -> str:
    """What per-client limits count by: an IPv4 address, or an IPv6 address's
    /64 (one home or phone network gets a whole /64 and can rotate through it)."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


class ContextLogger(logging.LoggerAdapter):
    """Appends the request, client and job ids that apply to each line."""

    def log(self, level, msg, *args, **kwargs):
        if not self.isEnabledFor(level):
            return
        # Format now, so the ids appended below (an address may hold "%") are
        # never read as format codes.
        if args:
            try:
                msg = str(msg) % args
            except (TypeError, ValueError):
                msg = f"{msg} {args!r}"
        msg, kwargs = self.process(msg, kwargs)
        self.logger.log(level, "%s", msg, **kwargs)

    def process(self, msg, kwargs):
        context = _context.get()
        kwargs["extra"] = {**kwargs.get("extra", {}), **{key: context.get(key, "-") for key in CONTEXT_KEYS}}
        present = {key: context[key] for key in CONTEXT_KEYS if context.get(key)}
        if present:
            msg = f"{msg} {fields_text(present)}"
        return msg, kwargs


def get_logger(name: str) -> ContextLogger:
    return ContextLogger(logging.getLogger(name), {})


_request_log = get_logger(log.name)


def _quiet(method: str, path: str, route: str) -> bool:
    return path in QUIET_PATHS or path.startswith(QUIET_PREFIXES) or (method, route) in QUIET_ROUTES


class RequestDiagnosticsMiddleware:
    """Give every HTTP response an ID (X-Request-ID) and log the request once.

    Always mint our own ID: Uvicorn rewrites scope.client from proxy headers,
    so the application cannot prove which peer supplied a request-ID header.
    Nginx logs both its edge ID and this response header.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        context = {"request_id": request_id, "client_ip": client_ip(scope), "fields": {}}
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        status: int | None = None
        error: BaseException | None = None
        error_body = bytearray()
        capture = False
        started = time.monotonic()

        async def correlated_send(message):
            nonlocal status, capture
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(n, v) for n, v in message.get("headers", []) if n.lower() != b"x-request-id"]
                if status >= 400:
                    names = {n.lower(): v for n, v in headers}
                    capture = (names.get(b"content-type", b"").startswith(b"application/json")
                               and b"content-encoding" not in names)
                message = {**message, "headers": headers + [(b"x-request-id", request_id.encode("ascii"))]}
            elif message["type"] == "http.response.body" and capture and len(error_body) < 2000:
                error_body.extend(message.get("body", b"")[: 2000 - len(error_body)])
            await send(message)

        with bind_context(context):
            try:
                await self.app(scope, receive, correlated_send)
            except BaseException as exc:
                error = exc
                raise
            finally:
                self._log(scope, context, status, error, error_body, started)

    @staticmethod
    def _log(scope, context, status, error, error_body, started) -> None:
        method = scope.get("method", "")
        path = scope.get("path", "/")
        route = getattr(scope.get("route"), "path", None) or path
        # ClientDisconnect: the client went away while its body was being read.
        crash = error if isinstance(error, Exception) and not isinstance(error, ClientDisconnect) else None
        cancelled = error is not None and crash is None  # the client went away
        if crash is not None:
            status = 500
        elif status is None:
            status = 499 if cancelled else 500
        if status < 400 and not cancelled and _quiet(method, path, route):
            return
        fields: dict[str, Any] = {"method": method, "route": route}
        if route != path:
            fields["path"] = path
        fields["status"] = status
        fields["duration_ms"] = (time.monotonic() - started) * 1000
        fields.update(context["fields"])
        if cancelled:
            fields["cancelled"] = True
        if status >= 400 and status != 499:
            query = scope.get("query_string", b"")
            if query:
                fields["query"] = query.decode("latin-1")
            detail = None
            if error_body:
                try:
                    detail = json.loads(bytes(error_body)).get("detail")
                except (ValueError, AttributeError):
                    # Not JSON as captured (cut at 2,000 bytes, say): keep its text.
                    detail = bytes(error_body).decode("utf-8", "replace")
            if detail:
                fields["detail"] = detail
            if crash is not None:
                fields["error_type"] = type(crash).__name__
            for name, value in scope.get("headers", []):
                if name == b"user-agent":
                    fields["ua"] = value.decode("latin-1")
                    break
        level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 and status != 499 else logging.INFO
        event = "request.failed" if level > logging.INFO else "request"
        log_event(_request_log, level, event, exc_info=crash, **fields)
