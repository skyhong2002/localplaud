#!/usr/bin/env python3
"""Bounded WSL container GPU recovery; run on the Docker host as its owner."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class UnsafeToRecover(RuntimeError):
    pass


def run(args, timeout=40):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise UnsafeToRecover("command unavailable or timed out") from None
    if result.returncode:
        # Never copy Docker environment, job payloads, or arbitrary stderr into logs.
        raise UnsafeToRecover("command failed")
    return result.stdout


class DockerWorker:
    def __init__(self, container, maintenance_lock):
        self.container = container
        self.maintenance_lock = maintenance_lock

    def inspect(self):
        data = json.loads(run([
            "docker", "inspect", self.container, "--format",
            '{{json .State}}',
        ]))
        if not data["Running"] or data.get("Paused") or data.get("Restarting"):
            raise UnsafeToRecover("container is not running normally")
        return datetime.fromisoformat(data["StartedAt"].replace("Z", "+00:00")).timestamp()

    def idle(self):
        code = """
import json, sqlite3
from pathlib import Path
from sqlalchemy.engine import make_url
from localplaud.config import get_settings
url = make_url(get_settings().store.database_url)
assert url.get_backend_name() == 'sqlite' and url.database != ':memory:'
path = Path(url.database).resolve()
lock = Path(str(path) + '.gpu-maintenance.lock')
stat = lock.stat()
c = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
n = c.execute("select count(*) from remote_jobs where status in ('queued','running')").fetchone()[0]
print(json.dumps({'active': n, 'lock_inode': stat.st_ino, 'lock_device': stat.st_dev}))
"""
        info = json.loads(run(["docker", "exec", self.container, "/opt/venv/bin/python", "-c", code]))
        stat = self.maintenance_lock.stat()
        if (info["lock_inode"], info["lock_device"]) != (stat.st_ino, stat.st_dev):
            raise UnsafeToRecover("maintenance lock does not match worker database")
        # Also protect ad-hoc GPU work not represented by the worker ledger.
        rows = run(["docker", "top", self.container, "-eo", "pid,comm"]).splitlines()[1:]
        processes = [row.split(maxsplit=1)[1].strip() for row in rows]
        return info["active"] == 0 and processes == ["localplaud"]

    def host_healthy(self):
        output = run([
            "/usr/lib/wsl/lib/nvidia-smi", "--query-gpu=name", "--format=csv,noheader",
        ])
        return bool(output.strip())

    def gpu_healthy(self):
        code = """
import json, torch
available = torch.cuda.is_available()
if available:
    x = torch.ones((8, 8), device='cuda')
    assert (x @ x)[0, 0].item() == 8
    torch.cuda.synchronize()
print(json.dumps({'available': available}))
"""
        # Import errors, OOM and timeouts are unknown failures, not permission
        # to restart. Only an explicit cuda.is_available() == False qualifies.
        result = json.loads(run([
            "docker", "exec", self.container, "/opt/speech/bin/python", "-c", code,
        ]))
        return result["available"] is True

    def restart(self):
        run(["docker", "restart", "--timeout", "60", self.container], timeout=90)

    @contextmanager
    def maintenance(self):
        # The worker creates this file. Do not create it on its behalf.
        with self.maintenance_lock.open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def check(worker, state, now, save, apply=False):
    started = worker.inspect()
    if state.get("container_started") != started:
        state.update(container_started=started, failures=0)
    if now - started < 120:
        return "startup_grace"
    if not worker.idle():
        state["failures"] = 0
        return "busy"
    if not worker.host_healthy():
        state["failures"] = 0
        return "host_gpu_unavailable"
    if worker.gpu_healthy():
        state["failures"] = 0
        return "healthy"
    state["failures"] = state.get("failures", 0) + 1
    if state["failures"] < 2:
        return "confirming_gpu_loss"
    attempts = [t for t in state.get("restart_attempts", []) if now - t < 86400]
    state["restart_attempts"] = attempts
    if attempts and now - attempts[-1] < 900:
        return "cooldown"
    if len(attempts) >= 3:
        return "daily_limit"
    if not apply:
        return "would_restart"
    # Healthy checks never block job admission. Take the exclusive lock only
    # for actual recovery, then recheck idle state while new jobs are excluded.
    with worker.maintenance():
        if worker.inspect() != started or not worker.idle():
            return "worker_changed"
        state["restart_attempts"] = attempts + [now]
        state["failures"] = 0
        save()  # Count even a failed restart/crash against the budget.
        worker.restart()
        return "restarted_verified" if worker.gpu_healthy() else "restarted_gpu_still_unavailable"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--maintenance-lock", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="allow bounded container restarts")
    args = parser.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.state.with_suffix(".lock").open("a") as execution_lock:
        try:
            fcntl.flock(execution_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        state = json.loads(args.state.read_text()) if args.state.exists() else {}

        def save():
            temporary = args.state.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, indent=2) + "\n")
            temporary.chmod(0o600)
            os.replace(temporary, args.state)

        try:
            status = check(
                DockerWorker(args.container, args.maintenance_lock), state,
                time.time(), save, apply=args.apply,
            )
        except BlockingIOError:
            status = "busy"
            state["failures"] = 0
        except (UnsafeToRecover, OSError, ValueError, KeyError):
            status = "check_failed_no_restart"
            state["failures"] = 0
        state.update(last_check=time.time(), last_status=status)
        save()
        print(json.dumps({"status": status, "failures": state.get("failures", 0)}))


if __name__ == "__main__":
    main()
