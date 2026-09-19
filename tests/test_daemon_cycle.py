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
