"""Background jobs: loading sources from iNaturalist and generating decks.

Jobs run in bounded thread pools inside the single Uvicorn process. The browser
polls ``/api/jobs/<id>`` for progress. Limits: concurrent and queued loads and
generations, active jobs per client, each client's daily share of iNaturalist
requests, a timeout that starts when the job does, and a TTL after which
finished jobs (and their .pptx) are deleted.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .diagnostics import bind_context, get_logger, log_event, snapshot
from .images import DownloadCancelled, ImageFetchError, MediaFetcher, prepare_for_slide, title_background
from .inaturalist import INatClient, INatError, SourceError, count_requests, source_params
from .models import Project
from .presentation import build_slide_plan, write_pptx
from .project import ProjectError, add_observations, refresh_project, safe_filename
from .ratelimit import BudgetExceeded
from .workspace import WorkspaceExpired, WorkspaceStore

log = get_logger(__name__)

EST_BYTES_PER_PHOTO = 1_500_000
MIB = 1024 ** 2
# A finished load's result holds every observation's metadata (tens of MB for a
# big project). It is dropped once the browser has fetched it, after a grace
# period in case that response was lost, or after LOAD_RESULT_TTL regardless.
RESULT_GRACE_SECONDS = 120
LOAD_RESULT_TTL = 15 * 60


class JobRejected(Exception):
    """Job could not be started (limits). Message is user-safe."""


class JobFailed(Exception):
    """Job failed with a user-safe message."""


class JobCancelled(Exception):
    pass


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Job:
    id: str
    kind: str
    client: str
    timeout: float = 0.0
    created: float = field(default_factory=time.time)
    started: float | None = None
    state: str = "queued"  # queued | running | done | error | cancelled
    phase: str = ""
    done: int = 0
    total: int = 0
    message: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    result: Any = None
    output: Path | None = None
    filename: str = ""
    finished: float | None = None
    deadline: float = 0.0  # set when the job starts running
    cancel_event: threading.Event = field(default_factory=threading.Event)
    delivered: float | None = None  # when a finished load's result was first sent
    result_dropped: bool = False
    log_context: dict[str, str] = field(default_factory=dict, repr=False)
    log_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def progress(self, phase: str, done: int, total: int, message: str = "") -> None:
        self.phase, self.done, self.total = phase, done, total
        if message:
            self.message = message
        self.check()

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled()
        if self.deadline and time.time() > self.deadline:
            raise JobFailed("This job took too long and was stopped. Try a smaller presentation.")

    def public(self) -> dict:
        data = {
            "id": self.id, "kind": self.kind, "state": self.state, "phase": self.phase,
            "done": self.done, "total": self.total, "message": self.message,
            "error": self.error, "warnings": self.warnings,
        }
        if self.state == "done":
            if self.kind == "generate":
                data["download_url"] = f"/api/jobs/{self.id}/download"
                data["filename"] = self.filename
            data["result"] = self.result
        return data


class JobManager:
    def __init__(self, settings: Settings, client: INatClient, media: MediaFetcher, workspaces: WorkspaceStore):
        self.settings = settings
        self.client = client
        self.media = media
        self.workspaces = workspaces
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._load_pool = ThreadPoolExecutor(settings.max_concurrent_loads, thread_name_prefix="load")
        self._gen_pool = ThreadPoolExecutor(settings.max_concurrent_generations, thread_name_prefix="gen")
        # iNaturalist requests made by each client today (in memory only).
        self._usage_day = ""
        self._usage: dict[str, int] = {}

    # -- bookkeeping ---------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        if not isinstance(job_id, str) or len(job_id) > 64:
            return None
        return self.jobs.get(job_id)

    def _active(self, kind: str | None = None, client: str | None = None) -> list[Job]:
        """Queued or running jobs. The caller holds the lock."""
        return [
            j for j in self.jobs.values()
            if j.state in ("queued", "running")
            and (kind is None or j.kind == kind)
            and (client is None or j.client == client)
        ]

    def counts(self) -> dict[str, int]:
        """Queued or running jobs by kind (for /healthz and the restart wrapper)."""
        with self._lock:
            active = self._active()
        return {
            "loading": sum(1 for j in active if j.kind == "load"),
            "generating": sum(1 for j in active if j.kind == "generate"),
        }

    def api_share(self) -> int:
        """iNaturalist requests one client may use per UTC day."""
        return int(self.client.budget.limit * self.settings.api_client_daily_share)

    def _client_usage(self, client: str) -> int:
        """Requests ``client`` made today. The caller holds the lock."""
        day = _utc_day()
        if day != self._usage_day:
            self._usage_day, self._usage = day, {}
        return self._usage.get(client, 0)

    def _record_usage(self, client: str, requests: int) -> None:
        if requests:
            with self._lock:
                self._usage[client] = self._client_usage(client) + requests

    def _submit(self, kind: str, client: str, fn: Callable[[Job], Any], timeout: int) -> Job:
        with self._lock:
            self._trim_results()
            if len(self._active(client=client)) >= self.settings.max_active_jobs_per_client:
                raise JobRejected("You already have a job running. Wait for it to finish or cancel it.")
            if kind == "generate" and len(self._active("generate")) >= self.settings.max_queued_generations:
                raise JobRejected("The server is busy generating other presentations. Please try again in a few minutes.")
            if kind == "load":
                if len(self._active("load")) >= self.settings.max_queued_loads:
                    raise JobRejected("The server is busy loading other projects. Please try again in a few minutes.")
                if self._client_usage(client) >= self.api_share():
                    raise JobRejected(
                        "You have used this site's share of today's iNaturalist requests. "
                        "Please try again tomorrow (UTC)."
                    )
            job = Job(id=secrets.token_urlsafe(16), kind=kind, client=client, timeout=timeout)
            request = snapshot()
            job.log_context = {
                "request_id": request.get("request_id", "-"),
                "client_ip": request.get("client_ip") or client,
                "job_id": job.id,
            }
            self.jobs[job.id] = job
        pool = self._gen_pool if kind == "generate" else self._load_pool
        try:
            pool.submit(self._run, job, fn)
        except RuntimeError:  # the pools are shut down: the server is stopping
            job.state, job.error, job.finished = "error", "The server is restarting.", time.time()
            raise JobRejected("The server is restarting. Please try again in a minute.") from None
        return job

    def _run(self, job: Job, fn: Callable[[Job], Any]) -> None:
        with bind_context(job.log_context):
            self._run_with_context(job, fn)

    def _run_with_context(self, job: Job, fn: Callable[[Job], Any]) -> None:
        with self._lock:
            if job.state != "queued":  # cancelled while it waited
                return
            job.state = "running"
            job.started = time.time()
            # The time limit counts from now, not from when it was queued.
            job.deadline = job.started + job.timeout if job.timeout else 0.0
        log_event(log, logging.INFO, "job.started", kind=job.kind, wait_ms=(job.started - job.created) * 1000)
        error_type = None
        crash = None
        with count_requests() as requests:
            try:
                job.check()
                job.result = fn(job)
                job.state = "done"
            except JobCancelled:
                job.state, job.error = "cancelled", "Cancelled."
            except (JobFailed, INatError, SourceError, BudgetExceeded, ImageFetchError) as exc:
                error_type = type(exc).__name__
                job.state, job.error = "error", str(exc)
            except WorkspaceExpired:
                error_type = "WorkspaceExpired"
                job.state, job.error = "error", "Your session data expired. Reload the sources and try again."
            except Exception as exc:
                error_type, crash = type(exc).__name__, exc
                job.state, job.error = "error", "Something went wrong on the server. Please try again."
            finally:
                job.finished = time.time()
                if job.state != "done" and job.output:
                    shutil.rmtree(job.output.parent, ignore_errors=True)
                    job.output = None
        self._record_usage(job.client, requests[0])
        self._log_finished(job, error_type, crash, api_requests=requests[0])

    def _log_finished(self, job: Job, error_type: str | None = None, crash: Exception | None = None, **extra) -> None:
        """One line per job with what it did: for usage figures and for debugging."""
        fields: dict[str, Any] = {"kind": job.kind, "state": job.state}
        if job.started:
            fields["wait_ms"] = (job.started - job.created) * 1000
            fields["run_ms"] = ((job.finished or time.time()) - job.started) * 1000
        if error_type:
            fields["error_type"] = error_type
        if job.state == "error":
            fields["error"] = job.error
        fields.update(job.log_fields)
        fields.update(extra)
        if job.warnings:
            fields["warnings"] = len(job.warnings)
        fields["api_left"] = self.client.budget.remaining()
        if job.kind == "generate":
            fields["media_hour_left_mb"] = self.media.budget.remaining()[0] // MIB
        level = logging.ERROR if crash else logging.WARNING if job.state == "error" else logging.INFO
        log_event(log, level, "job.finished", exc_info=crash, **fields)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        was_queued = False
        with self._lock:
            job.cancel_event.set()
            if job.state == "queued":
                # It has not started, so it ends now: it frees the client's
                # slot at once and the browser's poll sees "cancelled".
                job.state, job.error, job.finished = "cancelled", "Cancelled.", time.time()
                was_queued = True
        if was_queued:
            with bind_context(job.log_context):
                self._log_finished(job)
        if job.state == "done" and job.output:
            shutil.rmtree(job.output.parent, ignore_errors=True)
            job.output = None
        return True

    def shutdown(self) -> int:
        """Cancel every job so the worker threads stop at their next check. The
        process waits for them before it exits, and systemd stops waiting after
        30 seconds. Returns how many jobs were cancelled."""
        with self._lock:
            active = self._active()
            for job in active:
                job.cancel_event.set()
                if job.state == "queued":
                    job.state, job.error, job.finished = "cancelled", "The server restarted.", time.time()
        for pool in (self._load_pool, self._gen_pool):
            pool.shutdown(wait=False, cancel_futures=True)
        return len(active)

    def mark_delivered(self, job: Job) -> None:
        if job.kind == "load" and job.state == "done" and job.delivered is None:
            job.delivered = time.time()

    def _trim_results(self) -> None:
        """Drop finished loads' results once delivered. The caller holds the lock."""
        now = time.time()
        for job in self.jobs.values():
            if job.kind != "load" or job.state != "done" or job.result is None:
                continue
            if (job.delivered and now - job.delivered > RESULT_GRACE_SECONDS) or now - (job.finished or now) > LOAD_RESULT_TTL:
                job.result, job.result_dropped = None, True

    def cleanup(self) -> None:
        now = time.time()
        with self._lock:
            self._trim_results()
            for jid, job in list(self.jobs.items()):
                if job.finished and now - job.finished > self.settings.job_ttl_seconds:
                    if job.output:
                        shutil.rmtree(job.output.parent, ignore_errors=True)
                    del self.jobs[jid]
            live = {j.id for j in self.jobs.values()}
        for d in self.settings.jobs_dir.iterdir():
            if d.name not in live:
                shutil.rmtree(d, ignore_errors=True)

    # -- load ----------------------------------------------------------------

    def start_load(self, client: str, project: Project, workspace_id: str | None, refresh_ids: list[str] | None) -> Job:
        return self._submit("load", client, lambda job: self._load(job, project, workspace_id, refresh_ids),
                            self.settings.load_timeout_seconds)

    def _load(self, job: Job, project: Project, workspace_id: str | None, refresh_ids: list[str] | None) -> dict:
        s = self.settings
        prev = None
        if workspace_id and refresh_ids is not None:
            try:
                prev = self.workspaces.get(workspace_id)
            except WorkspaceExpired:
                prev = None
        enabled = [src for src in project.sources if src.enabled]
        if not project.sources:
            raise JobFailed("Add at least one source first.")
        if prev is None:
            to_query = enabled
        else:
            refresh = set(refresh_ids or [])
            to_query = [src for src in enabled if src.id in refresh or src.id not in prev["source_results"]]

        source_results: dict[str, list[int]] = {}
        observations: dict[int, dict] = {}
        if prev is not None:
            queried = {src.id for src in to_query}
            for src in project.sources:
                if src.id not in queried and src.id in prev["source_results"]:
                    source_results[src.id] = prev["source_results"][src.id]
            observations.update(prev["observations"])

        raw_all: list[dict] = []
        for n, src in enumerate(to_query, 1):
            params = source_params(src)
            label = src.label or src.url
            job.progress("search", 0, 0, f"Searching {label} ({n} of {len(to_query)})")

            def prog(done, total, label=label, n=n):
                job.progress("search", done, total, f"Fetching observations from {label} ({n} of {len(to_query)})")

            started = time.monotonic()
            raws, total = self.client.search(params, s.max_observations_per_source, prog, label, job.cancel_event.is_set)
            source_results[src.id] = [int(r["id"]) for r in raws if r.get("id")]
            raw_all.extend(raws)
            log_event(log, logging.INFO, "load.source", source=src.url, results=len(source_results[src.id]),
                      total=total, duration_ms=(time.monotonic() - started) * 1000)
            unique = set().union(*source_results.values())
            if len(unique) > s.max_observations_per_project:
                raise JobFailed(
                    f"Together these sources match {len(unique):,} observations; the limit is "
                    f"{s.max_observations_per_project:,}. Narrow a source or disable one."
                )

        if raw_all:
            job.progress("taxa", 0, 0, "Looking up taxonomy")
            observations.update(self.client.normalize_all(raw_all))

        # Keep what this project can use: what its sources returned, and its own
        # observations. An incremental load would otherwise carry along results
        # of sources that were removed since.
        keep = {st.id for st in project.observations}.union(*source_results.values())
        observations = {oid: obs for oid, obs in observations.items() if oid in keep}

        # Observations the project already has but no source returned this time:
        # look them up by id (batched) so they are refreshed, or marked unavailable.
        known_ids = [st.id for st in project.observations]
        missing = [oid for oid in known_ids if oid not in observations]
        if missing:
            job.progress("refresh", 0, len(missing), f"Refreshing {len(missing):,} saved observations")
            raws = self.client.fetch_by_ids(
                missing, lambda d, t: job.progress("refresh", d, t, "Refreshing saved observations"))
            observations.update(self.client.normalize_all(raws))

        project, summary = refresh_project(project, observations, source_results)
        first_load = not project.observations
        workspace = {"observations": observations, "source_results": source_results}
        new_count = summary["new"]
        if first_load and summary["new_ids"]:
            membership = {int(k): v for k, v in summary["membership"].items()}
            ids = summary["new_ids"]
            from .sorting import sort_ids
            sort = project.settings.sort
            key = sort.key if sort.key not in ("custom",) else "taxonomic"
            ordered = sort_ids(ids, observations, project, key, sort.direction, workspace)
            try:
                project = add_observations(project, ordered, observations, membership, placement="append",
                                           max_observations=s.max_observations_per_project)
            except ProjectError as exc:
                raise JobFailed(str(exc)) from None
            summary["auto_added"] = len(ids)
            summary["new_ids"], summary["new"] = [], 0
            summary["new_by_source"] = {k: 0 for k in summary["new_by_source"]}
        job.progress("saving", 0, 0, "Preparing workspace")
        wid = self.workspaces.create(observations, source_results)
        job.log_fields.update(
            sources=len(project.sources), queried=len(to_query), observations=len(observations),
            in_project=len(project.observations), new=new_count, unavailable=summary["unavailable"],
            first_load=first_load,
        )
        return {
            "workspace_id": wid,
            "project": project.model_dump(mode="json"),
            "observations": list(observations.values()),
            "source_results": {k: len(v) for k, v in source_results.items()},
            "summary": summary,
            "first_load": first_load,
        }

    # -- generate ------------------------------------------------------------

    def start_generate(self, client: str, project: Project, workspace_id: str) -> Job:
        ws = self.workspaces.get(workspace_id)  # fail fast if expired
        plan = build_slide_plan(project, ws["observations"], self.settings.max_image_slides)
        if plan["counts"]["images"] == 0:
            raise JobRejected("No photos are selected for the presentation.")
        if plan["counts"]["images"] > self.settings.max_image_slides:
            raise JobRejected(
                f"This presentation has {plan['counts']['images']:,} photo slides; the limit is "
                f"{self.settings.max_image_slides:,}. Deselect some photos or raise the favorites filter."
            )
        free = shutil.disk_usage(self.settings.jobs_dir).free
        need = plan["counts"]["images"] * EST_BYTES_PER_PHOTO * 2 + self.settings.min_free_disk_bytes
        if free < need:
            raise JobRejected("The server is low on temporary disk space. Please try again later.")
        return self._submit("generate", client, lambda job: self._generate(job, project, plan),
                            self.settings.generation_timeout_seconds)

    def _generate(self, job: Job, project: Project, plan: dict) -> dict:
        job_dir = self.settings.jobs_dir / job.id
        work = job_dir / "work"
        originals = work / "originals"  # hard links into the media cache
        work.mkdir(parents=True, exist_ok=True)
        filename = safe_filename(project.settings.title, ".pptx")
        out = job_dir / filename
        job.output = out
        job.filename = filename

        photos: dict[int, str] = {}
        for spec in plan["slides"]:
            if spec["kind"] == "image":
                photos[spec["photo_id"]] = spec["url"]
        title_spec = plan["slides"][0].get("photo")
        if title_spec:
            photos.setdefault(title_spec["photo_id"], title_spec["url"])

        uncached = sum(1 for pid in photos if not self.media.cached_path(pid))
        job.log_fields.update(slides=len(plan["slides"]), images=plan["counts"]["images"], photos=len(photos),
                              to_download=uncached)

        paths: dict[int, Path] = {}
        failures: list[str] = []
        total = len(photos)
        job.progress("download", 0, total, f"Downloading photos: 0 / {total}")
        # Set the expected download aside, so another deck starting now cannot
        # use the same allowance and make this one fail halfway.
        reservation = self.media.budget.reserve(uncached * EST_BYTES_PER_PHOTO)

        def fetch(pid: int, url: str) -> Path:
            # Queued downloads that start after a cancel stop at once; running
            # ones stop at their next chunk (see MediaFetcher.fetch_original).
            if job.cancel_event.is_set():
                raise JobCancelled()
            try:
                return self.media.fetch_into(pid, url, originals, cancelled=job.cancel_event.is_set,
                                             reservation=reservation)
            except DownloadCancelled:
                raise JobCancelled() from None

        try:
            with ThreadPoolExecutor(self.settings.media_concurrency, thread_name_prefix="media") as pool:
                futures = {pool.submit(copy_context().run, fetch, pid, url): pid for pid, url in photos.items()}
                done = 0
                try:
                    for fut in as_completed(futures):
                        pid = futures[fut]
                        try:
                            paths[pid] = fut.result()
                        except ImageFetchError as exc:
                            failures.append(str(exc))
                        done += 1
                        job.progress("download", done, total, f"Downloading photos: {done} / {total}")
                except BaseException:
                    for f in futures:
                        f.cancel()
                    raise
        finally:
            reservation.release()
        if failures:
            job.log_fields["photo_failures"] = len(failures)
            log_event(log, logging.WARNING, "generate.photo_failures", count=len(failures), examples=failures[:5])
            job.warnings.extend(failures[:20])
            if len(failures) > max(5, total // 10):
                raise JobFailed(f"{len(failures)} photos could not be downloaded from iNaturalist. Please try again later.")

        title_image = None
        if title_spec and title_spec["photo_id"] in paths:
            try:
                title_image = title_background(paths[title_spec["photo_id"]], work / "title.jpg",
                                               title_spec.get("position", "center"), rotation=title_spec.get("rotation", 0))
            except Exception as exc:  # Pillow raises many types for bad data
                job.log_fields["title_photo_error"] = type(exc).__name__
                job.warnings.append("The title background photo could not be read, so the title slide has no photo.")

        prepared = {}
        unreadable: list[int] = []
        images = [s for s in plan["slides"] if s["kind"] == "image"]
        n_images = len(images)
        for i, spec in enumerate(images, 1):
            pid = spec["photo_id"]
            if pid in paths and pid not in prepared:
                try:
                    prepared[pid] = prepare_for_slide(paths[pid], work)
                except Exception:
                    unreadable.append(pid)
            if i % 10 == 0 or i == n_images:
                job.progress("prepare", i, n_images, f"Checking photos: {i} / {n_images}")
        if unreadable:
            shown = ", ".join(str(pid) for pid in unreadable[:10]) + (", ..." if len(unreadable) > 10 else "")
            job.warnings.append(f"{len(unreadable)} photo(s) could not be read and were skipped (photo {shown}).")
            job.log_fields["unreadable"] = len(unreadable)

        n_slides = len(plan["slides"])
        result = write_pptx(
            plan, prepared, title_image, out,
            progress=lambda d, t: job.progress("build", d, t, f"Building slides: {d} / {t}"),
            save_progress=lambda d, t: job.progress("save", d, t, f"Writing PowerPoint file: {d} / {t} photos"),
            author=project.settings.presenter,
        )
        shutil.rmtree(work, ignore_errors=True)
        if result["skipped"]:
            job.warnings.append(
                f"{result['skipped']} photo slide(s) were skipped because the photo could not be downloaded or read.")
        size = out.stat().st_size
        job.log_fields.update(written_slides=result["slides"], bytes=size)
        return {"slides": result["slides"], "bytes": size, "planned_slides": n_slides}
