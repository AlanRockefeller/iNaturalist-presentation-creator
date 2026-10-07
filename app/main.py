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

log = logging.getLogger("presentations")
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
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.limit:
                        return await _too_large(send)
                except ValueError:
                    return await _too_large(send)
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
    """Real client address: trust X-Real-IP only from the local nginx proxy."""
    peer = request.client.host if request.client else "unknown"
    if peer in ("127.0.0.1", "::1"):
        real = request.headers.get("x-real-ip", "").strip()
        if real and len(real) <= 64:
            return real
    return peer


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
        self.api_budget.flush()


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or get_settings()
    svc = services or Services(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc.cleanup()
        svc.start_cleanup_thread()
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
        log.exception("event=request.unhandled path=%s", request.url.path)
        return JSONResponse({"detail": "Something went wrong on the server."}, status_code=500)

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
                "max_image_slides": settings.max_image_slides,
            },
        })

    @app.get("/healthz")
    async def healthz():
        # Counts only (no ids): the restart wrapper uses them to avoid killing
        # a deck that is being generated.
        active = [j for j in svc.jobs.jobs.values() if j.state in ("queued", "running")]
        return {
            "ok": True,
            "version": __version__,
            "jobs": {
                "loading": sum(1 for j in active if j.kind == "load"),
                "generating": sum(1 for j in active if j.kind == "generate"),
            },
        }

    @app.get("/robots.txt", response_class=HTMLResponse)
    async def robots():
        return HTMLResponse("User-agent: *\nDisallow: /api/\n", media_type="text/plain")

    # -- sources & projects --------------------------------------------------

    @app.post("/api/sources/parse")
    async def parse_source(body: ParseSourceRequest):
        try:
            parsed = build_source(body.url, body.username)
        except SourceError as exc:
            raise HTTPException(422, str(exc)) from None
        parsed.pop("params", None)
        return parsed

    @app.post("/api/project/validate")
    async def validate_project(request: Request):
        try:
            data = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "That file is not valid JSON.") from None
        try:
            project = await run_in_threadpool(load_project, data)
        except ProjectError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"project": project.model_dump(mode="json"), "observer_default": observer_default(project)}

    @app.post("/api/project/export")
    async def export_project(request: Request):
        try:
            data = json.loads(await request.body())
            project = await run_in_threadpool(load_project, data.get("project") if isinstance(data, dict) else None)
        except ProjectError as exc:
            raise HTTPException(422, str(exc)) from None
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, "Invalid project.") from None
        return project_to_json(project)

    # -- jobs ----------------------------------------------------------------

    @app.post("/api/load")
    def start_load(body: LoadRequest, request: Request):
        project = checked(body.project)
        try:
            job = svc.jobs.start_load(client_key(request), project, body.workspace_id, body.refresh_source_ids)
        except JobRejected as exc:
            raise HTTPException(429, str(exc)) from None
        return job.public()

    @app.post("/api/generate")
    def start_generate(body: ProjectRequest, request: Request):
        project = checked(body.project)
        try:
            job = svc.jobs.start_generate(client_key(request), project, body.workspace_id)
        except WorkspaceExpired:
            raise HTTPException(410, "Your session data expired. Reload the sources to continue.") from None
        except JobRejected as exc:
            raise HTTPException(429, str(exc)) from None
        return job.public()

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        job = svc.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "That job is not known (it may have expired).")
        return job.public()

    @app.delete("/api/jobs/{job_id}")
    async def job_cancel(job_id: str):
        if not svc.jobs.cancel(job_id):
            raise HTTPException(404, "That job is not known.")
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/download")
    async def job_download(job_id: str):
        job = svc.jobs.get(job_id)
        if not job or job.kind != "generate" or job.state != "done" or not job.output or not job.output.is_file():
            raise HTTPException(404, "That file is no longer available. Generate the presentation again.")
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
        return {"project": project.model_dump(mode="json")}

    @app.post("/api/plan")
    def plan(body: ProjectRequest):
        project = checked(body.project)
        ws = workspace(body.workspace_id)
        return build_slide_plan(project, ws["observations"], settings.max_image_slides)

    return app


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


_configure_logging()
