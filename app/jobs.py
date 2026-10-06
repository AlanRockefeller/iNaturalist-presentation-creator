"""Background jobs: loading sources from iNaturalist and generating decks.

Jobs run in bounded thread pools inside the single Uvicorn process. The browser
polls ``/api/jobs/<id>`` for progress. Limits: concurrent loads/generations,
queued generations, active jobs per client, a wall-clock timeout, and a TTL
after which finished jobs (and their .pptx) are deleted.
"""

from __future__ import annotations

import logging
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .images import ImageFetchError, MediaFetcher, prepare_for_slide, title_background
from .inaturalist import INatClient, INatError, SourceError, source_params
from .models import Project
from .presentation import build_slide_plan, write_pptx
from .project import add_observations, refresh_project, safe_filename
from .ratelimit import BudgetExceeded
from .workspace import WorkspaceExpired, WorkspaceStore

log = logging.getLogger(__name__)

JOB_ID_LEN = 22
EST_BYTES_PER_PHOTO = 1_500_000


class JobRejected(Exception):
    """Job could not be started (limits). Message is user-safe."""


class JobFailed(Exception):
    """Job failed with a user-safe message."""


class JobCancelled(Exception):
    pass


@dataclass
class Job:
    id: str
    kind: str
    client: str
    created: float = field(default_factory=time.time)
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
    deadline: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)

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

    # -- bookkeeping ---------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        if not isinstance(job_id, str) or len(job_id) > 64:
            return None
        return self.jobs.get(job_id)

    def _active(self, kind: str | None = None, client: str | None = None) -> list[Job]:
        return [
            j for j in self.jobs.values()
            if j.state in ("queued", "running")
            and (kind is None or j.kind == kind)
            and (client is None or j.client == client)
        ]

    def _submit(self, kind: str, client: str, fn: Callable[[Job], Any], timeout: int) -> Job:
        with self._lock:
            if len(self._active(client=client)) >= self.settings.max_active_jobs_per_client:
                raise JobRejected("You already have a job running. Wait for it to finish or cancel it.")
            if kind == "generate" and len(self._active("generate")) >= self.settings.max_queued_generations:
                raise JobRejected("The server is busy generating other presentations. Please try again in a few minutes.")
            job = Job(id=secrets.token_urlsafe(16), kind=kind, client=client)
            job.deadline = time.time() + timeout
            self.jobs[job.id] = job
        pool = self._gen_pool if kind == "generate" else self._load_pool
        pool.submit(self._run, job, fn)
        return job

    def _run(self, job: Job, fn: Callable[[Job], Any]) -> None:
        job.state = "running"
        try:
            job.check()
            job.result = fn(job)
            job.state = "done"
        except JobCancelled:
            job.state, job.error = "cancelled", "Cancelled."
        except (JobFailed, INatError, SourceError, BudgetExceeded, ImageFetchError) as exc:
            job.state, job.error = "error", str(exc)
        except WorkspaceExpired:
            job.state, job.error = "error", "Your session data expired. Reload the sources and try again."
        except Exception:
            log.exception("event=job.crashed kind=%s job=%s", job.kind, job.id[:8])
            job.state, job.error = "error", "Something went wrong on the server. Please try again."
        finally:
            job.finished = time.time()
            if job.state != "done" and job.output:
                shutil.rmtree(job.output.parent, ignore_errors=True)
                job.output = None

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        job.cancel_event.set()
        if job.state == "done" and job.output:
            shutil.rmtree(job.output.parent, ignore_errors=True)
            job.output = None
        return True

    def cleanup(self) -> None:
        now = time.time()
        with self._lock:
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
            to_query = [src for src in enabled if src.id in set(refresh_ids or []) or src.id not in prev["source_results"]]

        source_results: dict[str, list[int]] = {}
        observations: dict[int, dict] = {}
        if prev is not None:
            queried = {src.id for src in to_query}
            for src in project.sources:
                if src.id not in queried and src.id in prev["source_results"]:
                    source_results[src.id] = prev["source_results"][src.id]
            for oid, obs in prev["observations"].items():
                observations[oid] = obs

        raw_all: list[dict] = []
        unique: set[int] = set(observations)
        for n, src in enumerate(to_query, 1):
            params = source_params(src)
            label = src.label or src.url
            job.progress("search", 0, 0, f"Searching {label} ({n} of {len(to_query)})")

            def prog(done, total, label=label, n=n):
                job.progress("search", done, total, f"Fetching observations from {label} ({n} of {len(to_query)})")

            raws, _total = self.client.search(params, s.max_observations_per_source, prog, label, job.cancel_event.is_set)
            source_results[src.id] = [int(r["id"]) for r in raws if r.get("id")]
            raw_all.extend(raws)
            unique.update(source_results[src.id])
            if len(unique) > s.max_observations_per_project:
                raise JobFailed(
                    f"Together these sources match {len(unique):,} observations; the limit is "
                    f"{s.max_observations_per_project:,}. Narrow a source or disable one."
                )

        if raw_all:
            job.progress("taxa", 0, 0, "Looking up taxonomy")
            observations.update(self.client.normalize_all(raw_all))

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
        if first_load and summary["new_ids"]:
            membership = {int(k): v for k, v in summary["membership"].items()}
            ids = summary["new_ids"]
            from .sorting import sort_ids
            sort = project.settings.sort
            key = sort.key if sort.key not in ("custom",) else "taxonomic"
            ordered = sort_ids(ids, observations, project, key, sort.direction, workspace)
            project = add_observations(project, ordered, observations, membership, placement="append")
            summary["auto_added"] = len(ids)
            summary["new_ids"], summary["new"] = [], 0
            summary["new_by_source"] = {k: 0 for k in summary["new_by_source"]}
        job.progress("saving", 0, 0, "Preparing workspace")
        wid = self.workspaces.create(observations, source_results)
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
        self.media.budget.check(uncached * EST_BYTES_PER_PHOTO)

        paths: dict[int, Path] = {}
        failures: list[str] = []
        total = len(photos)
        job.progress("download", 0, total, f"Downloading photos: 0 / {total}")
        with ThreadPoolExecutor(self.settings.media_concurrency, thread_name_prefix="media") as pool:
            futures = {pool.submit(self.media.fetch_original, pid, url): pid for pid, url in photos.items()}
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
        if failures:
            job.warnings.extend(failures[:20])
            if len(failures) > max(5, total // 10):
                raise JobFailed(f"{len(failures)} photos could not be downloaded from iNaturalist. Please try again later.")

        title_image = None
        if title_spec and title_spec["photo_id"] in paths:
            title_image = title_background(paths[title_spec["photo_id"]], work / "title.jpg",
                                           title_spec.get("position", "center"), rotation=title_spec.get("rotation", 0))

        prepared = {}
        images = [s for s in plan["slides"] if s["kind"] == "image"]
        n_images = len(images)
        for i, spec in enumerate(images, 1):
            pid = spec["photo_id"]
            if pid in paths and pid not in prepared:
                try:
                    prepared[pid] = prepare_for_slide(paths[pid], work)
                except Exception:
                    job.warnings.append(f"Photo {pid} could not be read and was skipped.")
            if i % 10 == 0 or i == n_images:
                job.progress("prepare", i, n_images, f"Checking photos: {i} / {n_images}")

        n_slides = len(plan["slides"])
        result = write_pptx(
            plan, prepared, title_image, out,
            progress=lambda d, t: job.progress("build", d, t, f"Building slides: {d} / {t}"),
            save_progress=lambda d, t: job.progress("save", d, t, f"Writing PowerPoint file: {d} / {t} photos"),
            author=project.settings.presenter,
        )
        shutil.rmtree(work, ignore_errors=True)
        if result["skipped"]:
            job.warnings.append(f"{result['skipped']} photo slide(s) were skipped because the photo could not be downloaded.")
        size = out.stat().st_size
        return {"slides": result["slides"], "bytes": size, "planned_slides": n_slides}
