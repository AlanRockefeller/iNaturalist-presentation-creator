"""Job manager limits and lifecycle: cancelling, timeouts, shutdown, fair use."""

import threading
import time

import pytest

from app.jobs import JobRejected
from app.models import TitlePhoto
from tests.conftest import make_obs, wait_job
from tests.helpers import project, source


def _blocker(jobs, client="c1", kind="load"):
    """A running job that holds its worker until released."""
    release = threading.Event()
    started = threading.Event()

    def work(job):
        started.set()
        while not release.wait(0.01):
            job.check()
        return {}

    job = jobs._submit(kind, client, work, 60)
    assert started.wait(5)
    return job, release


def _wait_state(job, states, timeout=5):
    until = time.monotonic() + timeout
    while job.state not in states:
        assert time.monotonic() < until, job.state
        time.sleep(0.01)


@pytest.fixture
def one_worker(settings, services):
    services.jobs.shutdown()
    settings.max_concurrent_loads = 1
    from app.jobs import JobManager

    services.jobs = JobManager(settings, services.client, services.media, services.workspaces)
    yield services.jobs
    services.jobs.shutdown()


def test_cancelling_a_queued_job_ends_it_at_once_and_frees_the_slot(one_worker):
    jobs = one_worker
    running, release = _blocker(jobs, client="other")
    queued = jobs._submit("load", "c1", lambda job: {}, 60)
    assert queued.state == "queued"
    assert jobs.cancel(queued.id)
    assert queued.state == "cancelled" and queued.finished
    # The client's slots are free again although no worker has picked it up.
    a = jobs._submit("load", "c1", lambda job: {}, 60)
    b = jobs._submit("load", "c1", lambda job: {}, 60)
    release.set()
    for job in (running, a, b):
        _wait_state(job, ("done",))
    assert queued.state == "cancelled" and queued.started is None  # it never ran


def test_time_limit_counts_from_start_not_from_queueing(one_worker):
    jobs = one_worker
    running, release = _blocker(jobs, client="other")
    waited = jobs._submit("load", "c1", lambda job: job.check() or {"ok": True}, 1)
    time.sleep(1.3)  # longer than its limit, but only waiting
    release.set()
    _wait_state(waited, ("done", "error"))
    assert waited.state == "done", waited.error
    assert waited.deadline == pytest.approx(waited.started + 1)


def test_shutdown_cancels_running_and_queued_jobs(one_worker):
    jobs = one_worker
    running, _ = _blocker(jobs, client="other")
    queued = jobs._submit("load", "c1", lambda job: {}, 60)
    assert jobs.counts() == {"loading": 2, "generating": 0}
    assert jobs.shutdown() == 2
    assert queued.state == "cancelled"
    _wait_state(running, ("cancelled",))
    assert jobs.counts() == {"loading": 0, "generating": 0}
    with pytest.raises(JobRejected):
        jobs._submit("load", "c2", lambda job: {}, 60)


def test_global_cap_on_queued_loads(one_worker, settings):
    jobs = one_worker
    settings.max_queued_loads = 2
    running, release = _blocker(jobs, client="a")
    jobs._submit("load", "b", lambda job: {}, 60)
    with pytest.raises(JobRejected, match="busy loading"):
        jobs._submit("load", "c", lambda job: {}, 60)
    release.set()


def test_one_client_cannot_use_up_the_daily_api_budget(app_client, fake, settings):
    fake.add(make_obs(1, 47692, user="alice"))
    settings.api_client_daily_share = 2 / 10_000  # the test budget is 10,000 a day
    body = {"project": project(source("a", "alice")).model_dump(mode="json")}
    first = app_client.post("/api/load", json=body).json()
    assert wait_job(app_client, first["id"])["state"] == "done"  # 2 requests: search + taxa
    refused = app_client.post("/api/load", json=body)
    assert refused.status_code == 429 and "share of today's iNaturalist requests" in refused.json()["detail"]


def test_delivered_load_results_are_dropped_later(app_client, fake, services):
    fake.add(make_obs(1, 47692, user="alice"))
    body = {"project": project(source("a", "alice")).model_dump(mode="json")}
    job_id = app_client.post("/api/load", json=body).json()["id"]
    assert wait_job(app_client, job_id)["result"]["observations"]
    job = services.jobs.get(job_id)
    assert job.delivered
    assert app_client.get(f"/api/jobs/{job_id}").json()["result"]  # a lost response can be fetched again
    job.delivered -= 600
    services.jobs.cleanup()
    assert job.result is None
    gone = app_client.get(f"/api/jobs/{job_id}")
    assert gone.status_code == 410 and "expired" in gone.json()["detail"]


def _generate(app_client, fake, monkeypatch=None, **settings):
    fake.add(make_obs(1, 47692, user="alice", photos=((11, 800, 600), (12, 600, 800))),
             make_obs(2, 47602, user="alice", photos=((21, 640, 480),)))
    body = {"project": project(source("a", "alice")).model_dump(mode="json")}
    loaded = wait_job(app_client, app_client.post("/api/load", json=body).json()["id"])["result"]
    pr = loaded["project"]
    next(o for o in pr["observations"] if o["id"] == 1)["selected_photo_ids"] = [11, 12]
    pr["settings"].update(settings)
    job = app_client.post("/api/generate", json={"project": pr, "workspace_id": loaded["workspace_id"]}).json()
    return wait_job(app_client, job["id"])


def test_generation_survives_the_cache_being_trimmed_midway(app_client, fake, services, settings, monkeypatch):
    import app.jobs as jobs_module

    real_prepare = jobs_module.prepare_for_slide
    settings.media_cache_max_bytes = 0

    def prepare_after_trim(src, work):
        services.media.cleanup()  # the cleanup thread runs between download and writing
        return real_prepare(src, work)

    monkeypatch.setattr(jobs_module, "prepare_for_slide", prepare_after_trim)
    final = _generate(app_client, fake)
    assert final["state"] == "done", final["error"]
    assert final["result"]["slides"] == 4 and not final["warnings"]
    assert not list(settings.media_cache_dir.glob("*.jpg"))  # trimmed, yet every photo made it in


def test_unreadable_title_photo_is_a_warning_not_a_failure(app_client, fake, monkeypatch):
    import app.jobs as jobs_module

    def broken(*args, **kwargs):
        raise OSError("image file is truncated")

    monkeypatch.setattr(jobs_module, "title_background", broken)
    final = _generate(app_client, fake, title_photo=TitlePhoto(observation_id=1, photo_id=11).model_dump())
    assert final["state"] == "done", final["error"]
    assert any("title background photo could not be read" in w for w in final["warnings"])


def test_a_second_deck_cannot_take_the_first_ones_allowance(services):
    budget = services.media.budget
    budget.hourly = 100
    first = budget.reserve(80)
    with pytest.raises(Exception, match="last hour"):
        budget.reserve(30)
    first.release()
    budget.reserve(30).release()
