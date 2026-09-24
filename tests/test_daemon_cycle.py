"""Plaud outages must not prevent recovery of already downloaded audio."""

import pytest

from localplaud import cli
from localplaud.config import Settings


@pytest.mark.parametrize("sync_fails", [False, True])
def test_daemon_processes_local_queue_after_sync(monkeypatch, sync_fails):
    settings = Settings(_env_file=None)
    calls = []

    def poll(actual):
        assert actual is settings
        calls.append("sync")
        if sync_fails:
            raise RuntimeError("Plaud unavailable")

    def process(actual, *, daemon_owner):
        assert actual is settings
        assert daemon_owner == "owner"
        calls.append("process")
        return 3

    monkeypatch.setattr("localplaud.poller.poll.poll_once", poll)
    monkeypatch.setattr(cli, "process_automatic_pending", process)
    assert cli.run_processing_cycle(settings, daemon_owner="owner") == 3
    assert calls == ["sync", "process"]


def test_daemon_does_not_hide_processing_failures(monkeypatch):
    monkeypatch.setattr("localplaud.poller.poll.poll_once", lambda _: None)

    def process(*args, **kwargs):
        raise RuntimeError("worker failure")

    monkeypatch.setattr(cli, "process_automatic_pending", process)
    with pytest.raises(RuntimeError, match="worker failure"):
        cli.run_processing_cycle(Settings(_env_file=None))


class Scheduler:
    def __init__(self):
        self.jobs = {}
        self.wakes = []

    def add_job(self, callback, trigger, **kwargs):
        assert trigger == "interval"
        self.jobs[kwargs["id"]] = (callback, kwargs)

    def modify_job(self, job_id, **kwargs):
        self.wakes.append((job_id, kwargs))


def test_slow_processing_does_not_block_sync_or_duplicate_worker(monkeypatch):
    import threading

    from localplaud.worker.claims import current_processing_owner
    from localplaud.worker.daemon import DaemonJobs

    scheduler = Scheduler()
    jobs = DaemonJobs(Settings(_env_file=None), "owned", scheduler)
    entered, release = threading.Event(), threading.Event()
    work_calls, sync_calls = [], []

    def work(settings, *, daemon_owner):
        work_calls.append((daemon_owner, current_processing_owner()))
        entered.set()
        assert release.wait(3)
        return 1

    def sync(settings):
        sync_calls.append(current_processing_owner())
        return {"downloaded": 1}

    monkeypatch.setattr(cli, "process_automatic_pending", work)
    monkeypatch.setattr("localplaud.poller.poll.poll_once", sync)
    jobs.install()
    thread = threading.Thread(target=scheduler.jobs["worker"][0])
    thread.start()
    try:
        assert entered.wait(2)
        assert scheduler.jobs["sync"][0]() == {"downloaded": 1}
        assert scheduler.jobs["sync"][0]() == {"downloaded": 1}
        assert jobs.work() is None
        assert work_calls == [("owned", "owned")]
        assert sync_calls == ["owned", "owned"]
        assert [job for job, _ in scheduler.wakes] == ["worker", "worker"]
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert current_processing_owner() is None
    assert jobs.work() == 1


def test_background_failures_do_not_disable_other_loop_or_future_attempts(monkeypatch):
    from localplaud.worker.daemon import DaemonJobs

    jobs = DaemonJobs(Settings(_env_file=None), "owned", Scheduler())
    attempts = []

    def fail(_):
        raise RuntimeError("network down")

    def work(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("provider down")
        return 2

    monkeypatch.setattr("localplaud.poller.poll.poll_once", fail)
    monkeypatch.setattr(cli, "process_automatic_pending", work)
    assert jobs.sync() is None
    assert jobs.work() is None
    assert jobs.work() == 2
    monkeypatch.setattr("localplaud.poller.poll.poll_once", lambda _: {"downloaded": 1})
    assert jobs.sync() == {"downloaded": 1}


def test_owner_loss_stops_both_loops(monkeypatch):
    from localplaud.worker.daemon import DaemonJobs

    jobs = DaemonJobs(Settings(_env_file=None), "owned", Scheduler())
    monkeypatch.setattr("localplaud.poller.poll.refresh_daemon_owner", lambda owner: False)
    monkeypatch.setattr("localplaud.poller.poll.poll_once", lambda _: pytest.fail("lost owner"))
    monkeypatch.setattr(cli, "process_automatic_pending", lambda *a, **k: pytest.fail("lost owner"))
    jobs.heartbeat()
    assert jobs.stopped.is_set()
    assert jobs.sync() is None and jobs.work() is None


def test_disabled_sync_still_schedules_local_recovery(monkeypatch):
    from localplaud.worker.daemon import DaemonJobs

    settings = Settings(_env_file=None)
    settings.poller.enabled = False
    scheduler = Scheduler()
    jobs = DaemonJobs(settings, "owned", scheduler)
    jobs.install()
    assert set(scheduler.jobs) == {"worker", "daemon-heartbeat"}
    assert scheduler.jobs["worker"][1]["seconds"] <= 30
    monkeypatch.setattr("localplaud.poller.poll.poll_once", lambda _: pytest.fail("sync disabled"))
    monkeypatch.setattr(cli, "process_automatic_pending", lambda *a, **k: 1)
    assert jobs.sync() is None
    assert jobs.work() == 1
