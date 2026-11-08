"""Background jobs: loading sources from iNaturalist, generating decks and
writing presenter notes.

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
from collections import deque
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
from .notes import NotesCancelled, NotesError, subject_for
from .presentation import build_slide_plan, write_pptx
from .project import ProjectError, add_observations, own_logins, refresh_project, safe_filename
from . import rare as rare_finds
from .related import (
    LABELS as RELATED_LABELS, SearchCancelled, estimate_requests, pages_per_species, run_related_search,
)
from .ratelimit import BudgetExceeded, DailyBudget
from .workspace import WorkspaceExpired, WorkspaceStore

log = get_logger(__name__)

EST_BYTES_PER_PHOTO = 1_500_000
MIB = 1024 ** 2
# A finished load's result holds every observation's metadata (tens of MB for a
# big project). It is dropped once the browser has fetched it, after a grace
# period in case that response was lost, or after LOAD_RESULT_TTL regardless.
RESULT_GRACE_SECONDS = 120
LOAD_RESULT_TTL = 15 * 60
SHARE_USED_UP = ("You have used this site's share of today's iNaturalist requests. "
                 "Please try again tomorrow (UTC).")


class JobRejected(Exception):
    """Job could not be started (limits). Message is user-safe."""


class JobFailed(Exception):
    """Job failed with a user-safe message."""


class JobCancelled(Exception):
    pass


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass(eq=False)  # a job is equal only to itself
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
    # What the job does. It holds the whole project, so it is dropped as soon
    # as the job ends or is cancelled.
    work: Callable[["Job"], Any] | None = field(default=None, repr=False)

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
        elif self.kind == "notes" and self.result is not None:
            # What was written before a cancel, an error or the daily limit.
            data["result"] = self.result
        return data


class _Workers:
    """A fixed number of threads running jobs from a queue. Unlike a thread
    pool's queue, a cancelled job can be taken out of this one, so waiting work
    stays within the job limits however often jobs are started and cancelled."""

    def __init__(self, size: int, name: str, run: Callable[[Job], None]):
        self._size, self._name, self._run = size, name, run
        self._queue: deque[Job] = deque()
        self._cond = threading.Condition()
        self._threads: list[threading.Thread] = []
        self._stopped = False

    def submit(self, job: Job) -> None:
        with self._cond:
            if self._stopped:
                raise RuntimeError("workers stopped")
            self._queue.append(job)
            if len(self._threads) < self._size:  # threads start on first use
                thread = threading.Thread(target=self._loop, name=f"{self._name}-{len(self._threads)}", daemon=True)
                self._threads.append(thread)
                thread.start()
            self._cond.notify()

    def remove(self, job: Job) -> bool:
        with self._cond:
            try:
                self._queue.remove(job)
                return True
            except ValueError:  # a worker has already taken it
                return False

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._queue.clear()
            self._cond.notify_all()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stopped:
                    self._cond.wait()
                if not self._queue:
                    return
                job = self._queue.popleft()
            try:
                self._run(job)
            except Exception:  # _run records job errors itself; keep the worker alive
                log.exception("event=worker.crashed")


class JobManager:
    def __init__(self, settings: Settings, client: INatClient, media: MediaFetcher, workspaces: WorkspaceStore,
                 notes_writer=None, notes_budget: DailyBudget | None = None):
        self.settings = settings
        self.client = client
        self.media = media
        self.workspaces = workspaces
        # Writes presenter notes (notes.ClaudeCliWriter); None = notes are off.
        self.notes_writer = notes_writer
        self.notes_budget = notes_budget
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._workers = {
            "load": _Workers(settings.max_concurrent_loads, "load", self._run),
            "generate": _Workers(settings.max_concurrent_generations, "gen", self._run),
            "notes": _Workers(1, "notes", self._run),
        }
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
            "notes": sum(1 for j in active if j.kind == "notes"),
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

    def _charge(self, client: str) -> None:
        """Count one iNaturalist request against ``client``'s daily share, or
        refuse it once the share is used up. Called before every request, so
        neither one long load nor two at once can go past the share."""
        with self._lock:
            used = self._client_usage(client)
            if used >= self.api_share():
                raise BudgetExceeded(SHARE_USED_UP)
            self._usage[client] = used + 1

    def _submit(self, kind: str, client: str, fn: Callable[[Job], Any], timeout: int) -> Job:
        with self._lock:
            self._trim_results()
            if len(self._active(client=client)) >= self.settings.max_active_jobs_per_client:
                raise JobRejected("You already have a job running. Wait for it to finish or cancel it.")
            if kind == "generate" and len(self._active("generate")) >= self.settings.max_queued_generations:
                raise JobRejected("The server is busy generating other presentations. Please try again in a few minutes.")
            if kind == "notes" and len(self._active("notes")) >= self.settings.max_queued_notes:
                raise JobRejected("The server is busy writing other notes. Please try again in a few minutes.")
            if kind == "load":
                if len(self._active("load")) >= self.settings.max_queued_loads:
                    raise JobRejected("The server is busy loading other projects. Please try again in a few minutes.")
                if self._client_usage(client) >= self.api_share():
                    raise JobRejected(SHARE_USED_UP)
            job = Job(id=secrets.token_urlsafe(16), kind=kind, client=client, timeout=timeout, work=fn)
            request = snapshot()
            job.log_context = {
                "request_id": request.get("request_id", "-"),
                "client_ip": request.get("client_ip") or client,
                "job_id": job.id,
            }
            self.jobs[job.id] = job
        try:
            self._workers[kind].submit(job)
        except RuntimeError:  # the workers are stopped: the server is stopping
            job.state, job.error, job.finished, job.work = "error", "The server is restarting.", time.time(), None
            raise JobRejected("The server is restarting. Please try again in a minute.") from None
        return job

    def _run(self, job: Job) -> None:
        with bind_context(job.log_context):
            self._run_with_context(job)

    def _run_with_context(self, job: Job) -> None:
        with self._lock:
            if job.state != "queued" or job.work is None:  # cancelled while it waited
                return
            fn = job.work
            job.state = "running"
            job.started = time.time()
            # The time limit counts from now, not from when it was queued.
            job.deadline = job.started + job.timeout if job.timeout else 0.0
        log_event(log, logging.INFO, "job.started", kind=job.kind, wait_ms=(job.started - job.created) * 1000)
        error_type = None
        crash = None
        with count_requests(charge=lambda: self._charge(job.client)) as meter:
            try:
                job.check()
                job.result = fn(job)
                job.state = "done"
            except JobCancelled:
                job.state, job.error = "cancelled", "Cancelled."
            except (JobFailed, INatError, SourceError, BudgetExceeded, ImageFetchError, NotesError) as exc:
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
                job.work = None
                if job.state != "done" and job.output:
                    shutil.rmtree(job.output.parent, ignore_errors=True)
                    job.output = None
        self._log_finished(job, error_type, crash, api_requests=meter.count)

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
        if job.kind == "notes" and self.notes_budget is not None:
            fields["notes_left"] = self.notes_budget.remaining()
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
                # It has not started, so it ends now: it leaves the queue, frees
                # the client's slot and the project it held, and the browser's
                # poll sees "cancelled".
                self._workers[job.kind].remove(job)
                job.state, job.error, job.finished, job.work = "cancelled", "Cancelled.", time.time(), None
                was_queued = True
        if was_queued:
            with bind_context(job.log_context):
                self._log_finished(job)
        if job.state == "done" and job.output:
            shutil.rmtree(job.output.parent, ignore_errors=True)
            job.output = None
        return True

    def shutdown(self, wait: float = 5.0) -> int:
        """Cancel every job. Running ones stop at their next check (within a
        download chunk or a slide); this waits up to ``wait`` seconds for that,
        so the bytes they downloaded are counted before the budgets are saved.
        Returns how many jobs were cancelled."""
        with self._lock:
            active = self._active()
            for job in active:
                job.cancel_event.set()
                if job.state == "queued":
                    job.state, job.error, job.finished, job.work = "cancelled", "The server restarted.", time.time(), None
        for workers in self._workers.values():
            workers.stop()
        until = time.monotonic() + wait
        while time.monotonic() < until and any(job.state == "running" for job in active):
            time.sleep(0.05)
        return len(active)

    def status(self, job: Job) -> dict | None:
        """The job's public state, or None once its load result was dropped.
        Read under the lock, so cleanup cannot drop the result meanwhile."""
        with self._lock:
            if job.result_dropped:
                return None
            data = job.public()
            if job.kind == "load" and job.state == "done" and job.delivered is None:
                job.delivered = time.time()
        return data

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

    def start_load(self, client: str, project: Project, workspace_id: str | None, refresh_ids: list[str] | None,
                   related_taxa: list[int] | None = None) -> Job:
        return self._submit("load", client, lambda job: self._load(job, project, workspace_id, refresh_ids, related_taxa),
                            self.settings.load_timeout_seconds)

    def _load(self, job: Job, project: Project, workspace_id: str | None, refresh_ids: list[str] | None,
              related_taxa: list[int] | None = None) -> dict:
        s = self.settings
        earlier_ws = None
        if workspace_id:
            try:
                earlier_ws = self.workspaces.get(workspace_id)
            except WorkspaceExpired:
                earlier_ws = None
        # An incremental load reuses the workspace; a full refresh searches again.
        prev = earlier_ws if refresh_ids is not None else None
        enabled = [src for src in project.sources if src.enabled]
        if not project.sources:
            raise JobFailed("Add at least one source first.")
        refresh = set(refresh_ids or [])
        # "More photos" and "Rare finds" sources are searched only when asked for
        # by id (a Rare finds source also when it was never searched); on any
        # other load their observations are refreshed by id below.
        def searched_before(src) -> bool:
            return (prev is not None and src.id in prev["source_results"]) or any(
                src.id in st.source_ids for st in project.observations)

        searchable = [src for src in enabled if src.type not in ("related", "rare") or src.id in refresh
                      or (src.type == "rare" and not searched_before(src))]
        if prev is None:
            to_query = searchable
        else:
            to_query = [src for src in searchable if src.id in refresh or src.id not in prev["source_results"]]

        source_results: dict[str, list[int]] = {}
        observations: dict[int, dict] = {}
        if prev is not None:
            queried = {src.id for src in to_query}
            for src in project.sources:
                if src.id not in queried and src.id in prev["source_results"]:
                    source_results[src.id] = prev["source_results"][src.id]
            observations.update(prev["observations"])
        elif earlier_ws is not None:
            # A full refresh does not search "more photos" sources again. Keep
            # what they found: their membership, and the metadata of what still
            # waits for review (the project's own are refreshed by id below).
            in_project = {st.id for st in project.observations}
            searched = {src.id for src in to_query}
            for src in project.sources:
                if src.type != "related" or src.id in searched or src.id not in earlier_ws["source_results"]:
                    continue
                ids = earlier_ws["source_results"][src.id]
                source_results[src.id] = ids
                for oid in ids:
                    if oid not in in_project and oid in earlier_ws["observations"]:
                        observations[oid] = earlier_ws["observations"][oid]

        raw_all: list[dict] = []
        related_found: list[int] = []
        rare_counts: dict[str, dict[int, tuple[int, int]]] = {}  # this load's Rare finds searches
        selected_photos = {pid for st in project.observations for pid in st.selected_photo_ids}
        recheck: set[int] = set()
        for n, src in enumerate(to_query, 1):
            if src.type == "rare":
                raws, rare_counts[src.id] = self._search_rare(job, src, n, len(to_query))
                found = [int(r["id"]) for r in raws if r.get("id")]
                related_found.extend(found)  # shown on their own for review, like "more photos"
                earlier = prev["source_results"].get(src.id, []) if prev else []
                listed = [st.id for st in project.observations if src.id in st.source_ids]
                source_results[src.id] = list(dict.fromkeys(found + earlier + listed))
                raw_all.extend(raws)
                self._check_unique(source_results, enabled)
                continue
            if src.type == "related":
                raws = self._search_related(job, src, project, related_taxa, n, len(to_query))
                found = [int(r["id"]) for r in raws if r.get("id")]
                related_found.extend(found)
                # Membership only grows: a search of one species must not take
                # this source away from observations it found before.
                earlier = prev["source_results"].get(src.id, []) if prev else []
                listed = [st.id for st in project.observations if src.id in st.source_ids]
                source_results[src.id] = list(dict.fromkeys(found + earlier + listed))
                if related_taxa is None:
                    # A full search again: what it kept but did not find now
                    # is looked up by id, so it is current (or unavailable).
                    recheck.update(set(listed) - set(found))
                raw_all.extend(raws)
                self._check_unique(source_results, enabled)
                continue
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
            self._check_unique(source_results, enabled)

        for src in enabled:
            if src.type == "rare" and src.id not in source_results:
                # Not searched this time: it keeps the observations it found, so
                # it counts as loaded (Load must not offer to search it again).
                source_results[src.id] = [st.id for st in project.observations if src.id in st.source_ids]

        if raw_all:
            job.progress("taxa", 0, 0, "Looking up taxonomy")
            observations.update(self.client.normalize_all(raw_all, selected_photos))
        returned = {int(r["id"]) for r in raw_all if r.get("id")}
        for oid in recheck - returned:
            observations.pop(oid, None)  # cached metadata: refreshed by id below

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
            observations.update(self.client.normalize_all(raws, selected_photos))

        self._mark_rare(job, project, enabled, observations, source_results, rare_counts)
        project, summary = refresh_project(project, observations, source_results)
        # What this load's "more photos" search found, so the browser can show
        # just those (other new observations may still be waiting for review).
        summary["related_found"] = list(dict.fromkeys(related_found))
        # A first load adds everything at once, but what a "more photos" search
        # finds is always offered for review, even in an empty project.
        first_load = not project.observations and not any(src.type == "related" for src in to_query)
        workspace = {"observations": observations, "source_results": source_results}
        new_count = summary["new"]
        if first_load and summary["new_ids"]:
            membership = {int(k): v for k, v in summary["membership"].items()}
            ids = summary["new_ids"]
            from .sorting import sort_ids
            sort = project.settings.sort
            if enabled and all(src.type == "rare" for src in enabled) and sort.key == "taxonomic":
                # A deck of rare finds starts rarest first.
                sort = sort.model_copy(update={"key": "rarity", "direction": "asc", "base_key": "rarity",
                                               "base_direction": "asc"})
                project = project.model_copy(update={
                    "settings": project.settings.model_copy(update={"sort": sort})})
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

    def _check_unique(self, source_results: dict[str, list[int]], enabled) -> None:
        # Only enabled sources count: a disabled one keeps its results in the
        # workspace (re-enabling it is instant) but is not in the project.
        limit = self.settings.max_observations_per_project
        enabled_ids = {e.id for e in enabled}
        unique = set().union(*(ids for sid, ids in source_results.items() if sid in enabled_ids))
        if len(unique) > limit:
            raise JobFailed(
                f"Together these sources match {len(unique):,} observations; the limit is "
                f"{limit:,}. Narrow a source or disable one."
            )

    def _search_rare(self, job: Job, src, n: int, of: int) -> tuple[list[dict], dict[int, tuple[int, int]]]:
        """Run one Rare finds search (see rare.py) within the request budgets.
        Returns the observations to offer, and (worldwide, in the search)
        counts of the chosen species."""
        rare = src.rare
        need = rare_finds.estimate_requests(rare.top)
        with self._lock:
            share_left = self.api_share() - self._client_usage(job.client)
        left = min(self.client.budget.remaining(), share_left)
        if need > left:
            raise JobFailed(
                f"Finding the {rare.top:,} rarest species can take up to {need:,} iNaturalist requests, and only "
                f"{max(0, left):,} are left for you today. Ask for fewer species, or try again tomorrow (UTC).")
        label = src.label or "Rare finds"
        prefix = f"{label}" + (f" ({n} of {of})" if of > 1 else "")
        steps = {"scan": "reading the species in the search", "count": "counting observations worldwide",
                 "photos": "finding the observations"}

        def progress(phase: str, done: int, total: int) -> None:
            job.progress("search", done, total, f"{prefix}: {steps[phase]}")

        job.progress("search", 0, 0, prefix)
        started = time.monotonic()
        result = rare_finds.search(self.client, source_params(src), rare, progress, job.cancel_event.is_set)
        log_event(log, logging.INFO, "load.rare", source=src.url, taxa=result["taxa"], species=len(result["species"]),
                  results=len(result["raws"]), capped=result["capped"], duration_ms=(time.monotonic() - started) * 1000)
        if not result["species"]:
            raise JobFailed(f"{label}: iNaturalist found no observations for that search." if not result["taxa"] else
                            f"{label}: none of the observations in that search is identified to species, "
                            "so there is nothing to rank.")
        return result["raws"], {sp["id"]: (sp["world"], sp["local"]) for sp in result["species"]}

    def _mark_rare(self, job: Job, project: Project, enabled, observations: dict[int, dict],
                   source_results: dict[str, list[int]], scanned: dict[str, dict[int, tuple[int, int]]]) -> None:
        """Put worldwide and in-search counts on the observations of enabled Rare
        finds sources (``obs["rare"]``): from this load's search, or counted again
        for observations fetched fresh (two requests per 200 species). Ones kept
        from the previous workspace keep theirs."""
        counted_at = rare_finds.today()
        for src in enabled:
            if src.type != "rare" or src.rare is None:
                continue
            ids = set(source_results.get(src.id, [])) | {st.id for st in project.observations if src.id in st.source_ids}
            counts = dict(scanned.get(src.id, {}))
            need = {
                obs["taxon"]["id"] for oid in ids
                if (obs := observations.get(oid)) and "rare" not in obs and (obs.get("taxon") or {}).get("id")
            } - set(counts)
            if need:
                job.progress("counts", 0, 0, f"{src.label or 'Rare finds'}: counting observations worldwide")
                counts.update(rare_finds.counts_for(self.client, source_params(src), need, job.cancel_event.is_set))
            rare_finds.mark(observations, ids, src.id, counts, counted_at)

    def _search_related(self, job: Job, src, project: Project, related_taxa: list[int] | None,
                        n: int, of: int) -> list[dict]:
        """Run one "more photos" search (see related.py) within the request budgets."""
        listed = src.related.taxon_ids
        if related_taxa is None:
            taxa = list(listed)
        else:
            known = set(listed)
            taxa = [t for t in dict.fromkeys(related_taxa) if t in known]
        if not taxa:
            return []
        limit = self.settings.max_related_taxa
        if len(taxa) > limit:
            raise JobFailed(f"One search can cover at most {limit:,} species, not {len(taxa):,}. "
                            "Use Find on Select & Organize to list fewer.")
        whose = src.related.whose
        # The search itself, plus a little for the taxonomy and saved-observation
        # lookups after it, so it does not run out with the results in hand.
        need = estimate_requests(len(taxa), pages_per_species(whose, project.settings.related)) + 2
        with self._lock:
            share_left = self.api_share() - self._client_usage(job.client)
        left = min(self.client.budget.remaining(), share_left)
        if need > left:
            raise JobFailed(
                f"Searching {len(taxa):,} species can take up to {need:,} iNaturalist requests, and only "
                f"{max(0, left):,} are left for you today. Search fewer species, or try again tomorrow (UTC)."
            )
        what = "your photos" if whose == "mine" else "other people's photos"
        species = f"{len(taxa):,} species" if len(taxa) != 1 else "1 species"
        prefix = f"Searching {what} of {species}" + (f" ({n} of {of})" if of > 1 else "")
        job.progress("search", 0, need, prefix)
        started = time.monotonic()
        try:
            raws = run_related_search(
                self.client, whose, taxa, project.settings.related,
                lambda done, total: job.progress("search", done, total, f"{prefix}: {done:,} of up to {total:,} requests"),
                job.cancel_event.is_set,
                exclude=own_logins(project),
                max_photos=self.settings.max_photos_per_observation,
            )
        except SearchCancelled:
            raise JobCancelled() from None
        log_event(log, logging.INFO, "load.related", whose=whose, label=src.label or RELATED_LABELS[whose],
                  species=len(taxa), results=len(raws), duration_ms=(time.monotonic() - started) * 1000)
        return raws

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

    # -- presenter notes -----------------------------------------------------

    def start_notes(self, client: str, project: Project, workspace_id: str, ids: list[int] | None,
                    replace: bool, web_search: bool = False) -> Job:
        """Write presenter notes for ``ids`` (None: every observation with a
        slide). Observations that already have notes are skipped unless
        ``replace``. Notes are written once per name and given to every
        observation with that name."""
        if self.notes_writer is None or self.notes_budget is None:
            raise JobRejected("Presenter notes are not set up on this server.")
        ws = self.workspaces.get(workspace_id)  # fail fast if expired
        observations = ws["observations"]
        if ids is None:
            plan = build_slide_plan(project, observations)
            ids = [s["observation_id"] for s in plan["slides"] if s["kind"] == "image"]
        states = {st.id: st for st in project.observations}
        groups: dict[str, dict] = {}
        skipped = 0
        for oid in dict.fromkeys(ids):
            st, obs = states.get(oid), observations.get(oid)
            if st is None or obs is None or st.status != "active":
                continue
            if st.notes and not replace:
                skipped += 1
                continue
            subject = subject_for(obs, st)
            if not subject["name"]:  # identified only as "Life": nothing to write about
                continue
            groups.setdefault(subject["name"].casefold(), {"subject": subject, "ids": []})["ids"].append(oid)
        if not groups:
            raise JobRejected("Every observation already has notes." if skipped
                              else "There are no named observations to write notes for.")
        if len(groups) > self.settings.notes_max_per_job:
            raise JobRejected(
                f"These observations have {len(groups):,} different names; notes can be written for at most "
                f"{self.settings.notes_max_per_job:,} at a time.")
        if self.notes_budget.remaining() <= 0:
            raise JobRejected(self.notes_budget.message)
        work = list(groups.values())
        return self._submit("notes", client, lambda job: self._notes(job, work, skipped, web_search),
                            self.settings.notes_timeout_seconds)

    def _notes(self, job: Job, groups: list[dict], skipped: int, web_search: bool = False) -> dict:
        # Searching the web takes longer per name, so fewer names per call.
        size = max(1, self.settings.notes_web_batch_size if web_search else self.settings.notes_batch_size)
        batches = [groups[i:i + size] for i in range(0, len(groups), size)]
        total = len(groups)
        notes: dict[str, dict] = {}
        failed: list[str] = []
        unfunded = 0  # names left out because the daily allowance ran out
        stop = threading.Event()  # set when the job ends early, so running calls stop
        # Set once the daily allowance is used up: later calls are not started,
        # but calls already paid for finish and keep their notes.
        budget_used_up = threading.Event()

        def publish() -> dict:
            # A new object each time: a poll may be serialising the last one.
            result = {"notes": dict(notes), "failed": list(failed), "skipped": skipped}
            with self._lock:
                job.result = result
            return result

        def fund(n: int) -> int:
            """How many of ``n`` names the daily allowance pays for (all or
            what is left of it), counted against the allowance now."""
            if budget_used_up.is_set():
                return 0
            take = min(n, self.notes_budget.remaining())
            try:
                if take:
                    self.notes_budget.consume(take)
            except BudgetExceeded:  # another call took the rest meanwhile
                take = 0
            if take < n:
                budget_used_up.set()
            return take

        def write(batch: list[dict]) -> tuple[dict[str, str], int]:
            """Claude's notes by key, and how many names of the batch were paid for."""
            if job.cancel_event.is_set() or stop.is_set():
                raise JobCancelled()
            funded = fund(len(batch))
            if not funded:
                return {}, 0
            subjects = {f"n{i}": g["subject"] for i, g in enumerate(batch[:funded])}
            try:
                written = self.notes_writer.write_batch(
                    subjects, lambda: job.cancel_event.is_set() or stop.is_set(), web_search=web_search)
            except NotesCancelled:
                raise JobCancelled() from None
            return written, funded

        publish()
        job.progress("notes", 0, total, f"Writing notes: 0 / {total}")
        job.log_fields.update(names=total, observations=sum(len(g["ids"]) for g in groups), skipped=skipped,
                              web_search=web_search)
        done = 0
        try:
            with ThreadPoolExecutor(min(self.settings.notes_concurrency, len(batches)),
                                    thread_name_prefix="notes") as pool:
                futures = {pool.submit(copy_context().run, write, b): b for b in batches}
                try:
                    for fut in as_completed(futures):
                        batch = futures[fut]
                        try:
                            written, funded = fut.result()
                        except NotesError as exc:
                            if exc.fatal:
                                raise
                            written, funded = {}, len(batch)
                        if job.cancel_event.is_set():
                            # Stopped while Claude wrote: what it wrote now is not
                            # kept; batches finished before the stop are.
                            raise JobCancelled()
                        for i, g in enumerate(batch):
                            text = written.get(f"n{i}")
                            if i >= funded:
                                unfunded += 1
                            elif text:
                                for oid in g["ids"]:
                                    notes[str(oid)] = {"text": text, "name": g["subject"]["name"]}
                            else:
                                failed.append(g["subject"]["name"])
                        done += len(batch)
                        publish()
                        job.progress("notes", done, total, f"Writing notes: {done} / {total}")
                except BaseException:
                    stop.set()
                    for f in futures:
                        f.cancel()
                    raise
        finally:
            job.log_fields.update(written=len(notes), failed=len(failed), unfunded=unfunded)
        if failed:
            shown = ", ".join(failed[:10]) + (", ..." if len(failed) > 10 else "")
            job.warnings.append(f"Claude could not write notes for {len(failed)} name(s): {shown}.")
        if budget_used_up.is_set():
            # What was written stays in job.result (see Job.public).
            raise BudgetExceeded(self.notes_budget.message)
        return publish()
