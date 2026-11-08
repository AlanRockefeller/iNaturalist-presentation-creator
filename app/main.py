"""FastAPI application: routes, request limits, security headers.

Business logic lives in the service modules; this file wires HTTP to them.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import __version__
from .config import Settings, get_settings
from .diagnostics import (
    AlreadyLoggedFilter, RequestDiagnosticsMiddleware, annotate, client_bucket, client_ip, get_logger, log_event,
)
from .images import MediaFetcher
from .inaturalist import INatClient, SourceError, TaxaCache, build_source
from .jobs import JobManager, JobRejected
from .models import (
    AddObservationsRequest, LoadRequest, ParseSourceRequest, ProjectRequest, SortRequest,
)
from .presentation import build_slide_plan
from .project import (
    ProjectError, add_observations, ignore_observations, load_project, normalize_project,
    observer_default, project_to_json,
)
from .ratelimit import ByteBudget, DailyBudget, RateLimiter
from .sorting import sort_ids
from .workspace import WorkspaceExpired, WorkspaceStore

log = get_logger("presentations")
BASE = Path(__file__).resolve().parent

CSP = (
    "default-src 'self'; "
    "img-src 'self' data: blob: https://inaturalist-open-data.s3.amazonaws.com https://static.inaturalist.org; "
    "script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class BodyLimitMiddleware:
    """Reject request bodies over ``limit`` bytes, declared or streamed."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = chunked = False
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                declared = True
                try:
                    if int(value) > self.limit:
                        return await _too_large(send)
                except ValueError:
                    return await _too_large(send)
            elif name == b"transfer-encoding":
                chunked = True
        if chunked and not declared:
            # A streamed body is read here first. Raising from receive() inside
            # the app would not work: FastAPI turns that into a 400.
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > self.limit:
                    return await _too_large(send)
                if not message.get("more_body"):
                    break
            replayed = False

            async def replay():
                nonlocal replayed
                if replayed:
                    return await receive()
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            return await self.app(scope, replay, send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _too_large(send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send):
    body = json.dumps({"detail": "Request is too large."}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def client_key(request: Request) -> str:
    """Who per-client limits count: the address, or its /64 for IPv6."""
    return client_bucket(client_ip(request.scope))


class Services:
    def __init__(self, settings: Settings, client: INatClient | None = None, media: MediaFetcher | None = None):
        settings.ensure_dirs()
        self.settings = settings
        self.api_budget = DailyBudget(settings.api_daily_budget, settings.state_dir / "api-budget.json")
        self.media_budget = ByteBudget(settings.media_hourly_bytes, settings.media_daily_bytes,
                                       settings.state_dir / "media-budget.json")
        self.client = client or INatClient(
            settings,
            limiter=RateLimiter(settings.api_min_interval),
            budget=self.api_budget,
            taxa_cache=TaxaCache(settings.state_dir / "taxa-cache.json"),
        )
        self.media = media or MediaFetcher(settings, self.media_budget)
        self.workspaces = WorkspaceStore(settings.workspace_dir, settings.workspace_ttl_seconds)
        self.jobs = JobManager(settings, self.client, self.media, self.workspaces)
        self._stop = threading.Event()

    def cleanup(self) -> None:
        for fn in (self.jobs.cleanup, self.workspaces.cleanup, self.media.cleanup):
            try:
                fn()
            except Exception:
                log.exception("event=cleanup.failed step=%s", getattr(fn, "__qualname__", fn))

    def start_cleanup_thread(self, interval: int = 600) -> None:
        def loop():
            while not self._stop.wait(interval):
                self.cleanup()

        threading.Thread(target=loop, name="cleanup", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        # Cancelled jobs stop at their next progress check; shutdown() waits a
        # few seconds for that, so the budgets saved below include their bytes.
        cancelled = self.jobs.shutdown()
        self.api_budget.flush()
        self.media_budget.flush()
        log_event(log, logging.INFO, "app.stopping", jobs_cancelled=cancelled)


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    svc = services or Services(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc.cleanup()
        svc.start_cleanup_thread()
        log_event(log, logging.INFO, "app.started", version=__version__,
                  api_left=svc.api_budget.remaining(), workspaces=len(list(settings.workspace_dir.glob("*.json.gz"))))
        yield
        svc.stop()

    app = FastAPI(title="Dikarya Presentations", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.services = svc
    app.add_middleware(GZipMiddleware, minimum_size=2048)
    app.add_middleware(BodyLimitMiddleware, limit=settings.max_body_bytes)
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    templates = Jinja2Templates(directory=str(BASE / "templates"))

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=604800"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errs = exc.errors()
        first = errs[0] if errs else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        return JSONResponse({"detail": f"Invalid request ({where or 'body'}: {first.get('msg', 'bad value')})."}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        # ServerErrorMiddleware runs this after the request middleware, which
        # has already logged the exception; give the 500 the same request ID.
        return JSONResponse(
            {"detail": "Something went wrong on the server."}, status_code=500,
            headers={"X-Request-ID": getattr(request.state, "request_id", "-")},
        )

    def workspace(wid: str) -> dict:
        try:
            return svc.workspaces.get(wid)
        except WorkspaceExpired:
            raise HTTPException(410, "Your session data expired. Reload the sources to continue.") from None

    def checked(project) -> object:
        try:
            return normalize_project(project)
        except ProjectError as exc:
            raise HTTPException(422, str(exc)) from None

    # -- pages ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request, "index.html", {
            "version": __version__,
            "limits": {
                "max_sources": settings.max_sources,
                "max_observations": settings.max_observations_per_project,
                "max_observations_per_source": settings.max_observations_per_source,
                "max_image_slides": settings.max_image_slides,
            },
        })

    @app.get("/healthz")
    async def healthz():
        # Counts only (no ids): the restart wrapper uses them to avoid killing
        # a deck that is being generated.
        return {"ok": True, "version": __version__, "jobs": svc.jobs.counts()}

    @app.get("/robots.txt", response_class=HTMLResponse)
    async def robots():
        return HTMLResponse("User-agent: *\nDisallow: /api/\n", media_type="text/plain")

    # -- sources & projects --------------------------------------------------

    @app.post("/api/sources/parse")
    async def parse_source(body: ParseSourceRequest):
        annotate(input=body.url, username=body.username or None)
        try:
            parsed = build_source(body.url, body.username)
        except SourceError as exc:
            raise HTTPException(422, str(exc)) from None
        parsed.pop("params", None)
        annotate(type=parsed["type"], source=parsed["url"])
        return parsed

    @app.post("/api/project/validate")
    async def validate_project(request: Request):
        raw = await request.body()
        annotate(bytes=len(raw))
        try:
            data = await run_in_threadpool(json.loads, raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "That file is not valid JSON.") from None
        if isinstance(data, dict):
            annotate(schema=data.get("schema_version"), app_version=data.get("app_version"))
        try:
            project = await run_in_threadpool(load_project, data)
        except ProjectError as exc:
            raise HTTPException(422, str(exc)) from None
        annotate(sources=len(project.sources), observations=len(project.observations))
        return {"project": project.model_dump(mode="json"), "observer_default": observer_default(project)}

    @app.post("/api/project/export")
    async def export_project(request: Request):
        try:
            data = await run_in_threadpool(json.loads, await request.body())
            project = await run_in_threadpool(load_project, data.get("project") if isinstance(data, dict) else None)
        except ProjectError as exc:
            raise HTTPException(422, str(exc)) from None
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "Invalid project.") from None
        annotate(sources=len(project.sources), observations=len(project.observations))
        return project_to_json(project)

    # -- jobs ----------------------------------------------------------------

    @app.post("/api/load")
    def start_load(body: LoadRequest, request: Request):
        project = checked(body.project)
        annotate(sources=sum(1 for s in project.sources if s.enabled), observations=len(project.observations),
                 refresh=body.refresh_source_ids if body.workspace_id else None)
        try:
            job = svc.jobs.start_load(client_key(request), project, body.workspace_id, body.refresh_source_ids)
        except JobRejected as exc:
            raise HTTPException(429, str(exc)) from None
        annotate(job_id=job.id)
        return job.public()

    @app.post("/api/generate")
    def start_generate(body: ProjectRequest, request: Request):
        project = checked(body.project)
        annotate(observations=len(project.observations), title=project.settings.title or None)
        try:
            job = svc.jobs.start_generate(client_key(request), project, body.workspace_id)
        except WorkspaceExpired:
            raise HTTPException(410, "Your session data expired. Reload the sources to continue.") from None
        except JobRejected as exc:
            raise HTTPException(429, str(exc)) from None
        annotate(job_id=job.id)
        return job.public()

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        job = svc.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "That job is not known (it may have expired).")
        data = svc.jobs.status(job)
        if data is None:
            raise HTTPException(410, "These results have expired. Load the sources again.")
        return data

    @app.delete("/api/jobs/{job_id}")
    async def job_cancel(job_id: str):
        job = svc.jobs.get(job_id)
        annotate(job_id=job_id, kind=job.kind if job else None, state=job.state if job else None)
        if not svc.jobs.cancel(job_id):
            raise HTTPException(404, "That job is not known.")
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/download")
    async def job_download(job_id: str):
        job = svc.jobs.get(job_id)
        if not job or job.kind != "generate" or job.state != "done" or not job.output or not job.output.is_file():
            raise HTTPException(404, "That file is no longer available. Generate the presentation again.")
        annotate(bytes=job.output.stat().st_size)
        return FileResponse(
            job.output,
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            filename=job.filename,
        )

    # -- organising ----------------------------------------------------------

    @app.post("/api/sort")
    def sort(body: SortRequest):
        project = checked(body.project)
        ws = workspace(body.workspace_id)
        annotate(key=body.key, direction=body.direction, grouping=project.settings.grouping,
                 observations=len(project.order))
        order = sort_ids(project.order, ws["observations"], project, body.key, body.direction, ws, body.seed)
        return {"order": order}

    @app.post("/api/observations/add")
    def add(body: AddObservationsRequest):
        project = checked(body.project)
        ws = workspace(body.workspace_id)
        # Only sources still in the project count. An observation that only a
        # removed source matched is not added; it would otherwise end up with no
        # source and show in every view.
        order_ids = {s.id: i for i, s in enumerate(project.sources)}
        membership: dict[int, list[str]] = {}
        for sid, ids in ws["source_results"].items():
            if sid not in order_ids:
                continue
            for oid in ids:
                membership.setdefault(oid, []).append(sid)
        for oid in membership:
            membership[oid].sort(key=lambda s: order_ids[s])
        observation_ids = [i for i in body.observation_ids if i in membership]
        selected = {}
        for k, v in body.selected_photo_ids.items():
            if str(k).isdigit():
                selected[int(k)] = [int(p) for p in v][:200]
        try:
            project = add_observations(project, observation_ids, ws["observations"], membership,
                                       selected, body.placement, ws, settings.max_observations_per_project)
        except ProjectError as exc:
            raise HTTPException(422, str(exc)) from None
        if body.ignore_ids:
            project = ignore_observations(project, body.ignore_ids)
        annotate(added=len(observation_ids), ignored=len(body.ignore_ids), placement=body.placement,
                 observations=len(project.observations))
        return {"project": project.model_dump(mode="json")}

    @app.post("/api/plan")
    def plan(body: ProjectRequest):
        project = checked(body.project)
        ws = workspace(body.workspace_id)
        plan = build_slide_plan(project, ws["observations"], settings.max_image_slides)
        annotate(slides=plan["counts"]["total"], images=plan["counts"]["images"])
        return plan

    # Last added user middleware is outermost, covering body-limit rejects too.
    app.add_middleware(RequestDiagnosticsMiddleware)
    return app


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Lines that do not help debug, measure use or spot attacks:
    # httpx logs every iNaturalist request and photo download at INFO,
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Uvicorn's access log repeats the request middleware's line with less in it,
    logging.getLogger("uvicorn.access").addFilter(lambda record: False)
    # and Uvicorn logs a second traceback for crashes the middleware has logged.
    logging.getLogger("uvicorn.error").addFilter(AlreadyLoggedFilter())


_configure_logging()
