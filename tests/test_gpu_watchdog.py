"""Recovery is bounded and cannot race a new worker job admission."""

import importlib.util
from contextlib import nullcontext
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "gpu_watchdog", Path(__file__).parents[1] / "scripts/maintenance/gpu_watchdog.py",
)
watchdog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watchdog)


class Worker:
    active = False
    host = True
    gpu = False
    restarts = 0

    def maintenance(self):
        return nullcontext()

    def inspect(self):
        return 1

    def idle(self):
        return not self.active

    def host_healthy(self):
        return self.host

    def gpu_healthy(self):
        return self.gpu

    def restart(self):
        self.restarts += 1
        self.gpu = True


def test_repeated_loss_recovers_and_records_before_restart():
    worker, state, saved = Worker(), {}, []
    def save():
        saved.append(dict(state))
    assert watchdog.check(worker, state, 1000, save, True) == "confirming_gpu_loss"
    assert watchdog.check(worker, state, 1120, save, True) == "restarted_verified"
    assert saved[0]["restart_attempts"] == [1120]
    assert worker.restarts == 1


@pytest.mark.parametrize("attribute,value,expected", [
    ("active", True, "busy"),
    ("host", False, "host_gpu_unavailable"),
    ("gpu", True, "healthy"),
])
def test_safe_skips_reset_failure_streak(attribute, value, expected):
    worker = Worker()
    setattr(worker, attribute, value)
    state = {"container_started": 1, "failures": 5}
    assert watchdog.check(worker, state, 1000, lambda: None, True) == expected
    assert state["failures"] == 0
    assert worker.restarts == 0


@pytest.mark.parametrize("history,expected", [
    ([950], "cooldown"),
    ([100, 500, 900], "daily_limit"),
])
def test_restart_limits(history, expected):
    worker = Worker()
    now = 1000 if expected == "cooldown" else 2000
    state = {"container_started": 1, "failures": 2, "restart_attempts": history}
    assert watchdog.check(worker, state, now, lambda: None, True) == expected
    assert worker.restarts == 0


def test_check_only_and_startup_do_not_restart():
    worker = Worker()
    state = {"container_started": 1, "failures": 2}
    assert watchdog.check(worker, state, 100, lambda: None, True) == "startup_grace"
    assert watchdog.check(worker, state, 1000, lambda: None) == "would_restart"
    assert worker.restarts == 0


def test_busy_final_recheck_prevents_restart():
    worker = Worker()
    calls = iter([True, False])
    worker.idle = lambda: next(calls)
    state = {"container_started": 1, "failures": 2}
    assert watchdog.check(worker, state, 1000, lambda: None, True) == "worker_changed"
    assert worker.restarts == 0


def test_failed_restart_consumes_budget():
    worker = Worker()
    def fail():
        raise watchdog.UnsafeToRecover("failed")
    worker.restart = fail
    state, saved = {"container_started": 1, "failures": 2}, []
    with pytest.raises(watchdog.UnsafeToRecover):
        watchdog.check(worker, state, 1000, lambda: saved.append(dict(state)), True)
    assert saved[0]["restart_attempts"] == [1000]


def test_admission_and_maintenance_are_mutually_exclusive(monkeypatch, tmp_path):
    import fcntl
    from types import SimpleNamespace

    from fastapi import HTTPException

    from localplaud.remote import server

    database = tmp_path / "worker.db"
    monkeypatch.setattr(server, "get_settings", lambda: SimpleNamespace(
        store=SimpleNamespace(database_url=f"sqlite:///{database}"),
    ))
    with server.gpu_admission_lock():
        with Path(str(database) + ".gpu-maintenance.lock").open("r+") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with Path(str(database) + ".gpu-maintenance.lock").open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(HTTPException) as error:
            with server.gpu_admission_lock():
                pytest.fail("admitted during restart")
        assert error.value.status_code == 503
    with server.gpu_admission_lock():
        pass


def test_docker_idle_checks_ledger_processes_and_shared_inode(monkeypatch, tmp_path):
    import json

    lock = tmp_path / "maintenance.lock"
    lock.touch()
    stat = lock.stat()
    info = {"active": 0, "lock_inode": stat.st_ino, "lock_device": stat.st_dev}
    processes = "PID COMMAND\n123 localplaud\n"
    monkeypatch.setattr(watchdog, "run", lambda args: (
        json.dumps(info) if args[1] == "exec" else processes
    ))
    worker = watchdog.DockerWorker("worker", lock)
    assert worker.idle()
    info["active"] = 1
    assert not worker.idle()
    info["active"] = 0
    processes += "124 python\n"
    assert not worker.idle()
    info["lock_inode"] += 1
    with pytest.raises(watchdog.UnsafeToRecover):
        worker.idle()
