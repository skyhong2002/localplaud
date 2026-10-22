"""Opt-in local maintenance admission and aggregate drain receipts.

The CLI never signals services, reads SQLite, or starts providers. A persistent
request pauses new work; admitted work finishes before a normal supervisor stop.
"""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime

_admitted = contextvars.ContextVar("maintenance_admitted", default=False)
_gates = {}
_gates_lock = threading.Lock()


class MaintenancePaused(RuntimeError):
    pass


@contextlib.contextmanager
def directory(path, *, create=False):
    """Private control leaf, opened through canonical parent descriptors."""
    path = Path(path).absolute()
    # Configured parent paths can legitimately use macOS /var or /tmp aliases.
    # Canonicalize that parent; the control leaf and every opened canonical
    # component still use NOFOLLOW and the leaf must be owned/private.
    path = path.parent.resolve() / path.name
    if ".." in path.parts:
        raise ValueError("unsafe maintenance directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for i, part in enumerate(path.parts[1:]):
            if create and i == len(path.parts) - 2:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("maintenance directory must be owned private0700")
        yield fd
    finally:
        os.close(fd)


def read_json(fd, name):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        before = os.fstat(handle)
        if not (stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and before.st_uid == os.getuid() and stat.S_IMODE(before.st_mode) == 0o600
                and before.st_size <= 4096):
            raise ValueError("unsafe maintenance record")
        raw = os.read(handle, 4097)
        # Ignore access time: the read itself may bump it.
        def identity(st):
            return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_uid,
                    st.st_size, st.st_mtime_ns, st.st_ctime_ns)

        if len(raw) != before.st_size or identity(os.fstat(handle)) != identity(before):
            raise ValueError("maintenance record changed")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("invalid maintenance record")
        return value
    finally:
        os.close(handle)


def write_json(fd, name, value, *, exclusive=False):
    temporary = name if exclusive else ".receipt-" + uuid.uuid4().hex
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(value, stream, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not exclusive:
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        if not exclusive:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=fd)


def process_birth(pid):
    with subprocess.Popen(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          text=True) as child:
        try:
            output, _ = child.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()  # Only our own bounded ps child.
            child.communicate()
            raise ValueError("maintenance process identity unavailable") from None
    if child.returncode or not output.strip():
        raise ValueError("maintenance process not alive")
    return output.strip()


class Ticket:
    def __init__(self, gate):
        self.gate = gate
        self.closed = False

    @contextlib.contextmanager
    def activate(self):
        token = _admitted.set(True)
        try:
            yield
        finally:
            _admitted.reset(token)
            self.close()

    def close(self):
        with self.gate.lock:
            if not self.closed:
                self.closed = True
                self.gate.active -= 1


class Gate:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.lock = threading.Lock()
        self.active = 0
        self.serving = False
        self.instance = uuid.uuid4().hex
        self.reporter = None
        self.report_stop = threading.Event()

    def request(self):
        try:
            with directory(self.path) as fd:
                request = read_json(fd, "request.json")
                if not re.fullmatch("[0-9a-f]{32}", request.get("request_id", "")):
                    raise ValueError("invalid maintenance request")
                return request
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            # An unsafe/corrupt gate is paused, never silently interpreted as resume.
            return {"request_id": None}

    def reserve(self, *, inherit=True, continuation=False):
        with self.lock:
            if not continuation and not (inherit and _admitted.get()) and self.request() is not None:
                raise MaintenancePaused("Service is draining for maintenance; try again after resume.")
            self.active += 1
            return Ticket(self)

    @contextlib.contextmanager
    def activity(self):
        with self.reserve().activate():
            yield

    def queued(self, target, *args, **kwargs):
        """A queue item is new admission even inside an admitted daemon batch."""
        try:
            ticket = self.reserve(inherit=False)
        except MaintenancePaused:
            return False
        with ticket.activate():
            return target(*args, **kwargs)

    def snapshot(self):
        with self.lock:
            request = self.request()
            return {"request_id": request["request_id"] if request else None,
                    "paused": request is not None, "active": self.active,
                    "serving": self.serving,
                    "quiescent": bool(request and request["request_id"] and self.serving
                                      and self.active == 0),
                    "pid": os.getpid(), "instance": self.instance,
                    "observed_at_utc": datetime.now(UTC).isoformat()}

    def start_reporting(self, role):
        if role not in ("main", "voice") or self.reporter is not None:
            raise ValueError("invalid or duplicate maintenance reporter")
        self.report_stop.clear()
        birth = process_birth(os.getpid())

        def report():
            # Also recover explicitly deferred jobs after a process restart.
            # This never starts unrelated historical jobs when auto-process is off.
            resume_deferred = True
            last_resumed = None
            retry_lookup = threading.Event()
            retry_after = 0.0
            while not self.report_stop.is_set():
                value = self.snapshot()
                if value["paused"]:
                    resume_deferred = True
                    value.update(role=role, process_birth=birth)
                    try:
                        with directory(self.path) as fd:
                            write_json(fd, role + ".json", value)
                    except (OSError, ValueError):
                        pass  # Missing/failing receipts can never authorize a stop.
                elif role == "main" and value["serving"]:
                    try:
                        with directory(self.path) as fd:
                            resumed = read_json(fd, "resume.json").get("request_id")
                    except (OSError, ValueError, TypeError):
                        resumed = None
                    if retry_lookup.is_set() and time.monotonic() >= retry_after:
                        retry_lookup.clear()
                        resume_deferred = True
                    resume_deferred |= resumed is not None and resumed != last_resumed
                    if not resume_deferred:
                        self.report_stop.wait(0.25)
                        continue
                    try:
                        from .remote.server import DeferredScanUnavailable, resume_deferred_jobs

                        def dispatch():
                            try:
                                resume_deferred_jobs()
                            except DeferredScanUnavailable:
                                # Only a failed initial read is automatically retried;
                                # never retry an unknown provider execution outcome.
                                retry_lookup.set()

                        tracked_thread(target=dispatch, daemon=True,
                                       name="maintenance-remote-resume").start()
                        resume_deferred = False
                        last_resumed = resumed
                        retry_after = time.monotonic() + 1.0
                    except MaintenancePaused:
                        pass  # A concurrent begin is a fresh persistent pause.
                self.report_stop.wait(0.25)

        self.reporter = threading.Thread(target=report, daemon=True, name="maintenance-status")
        self.reporter.start()

    def close(self):
        self.serving = False
        self.report_stop.set()
        if self.reporter:
            self.reporter.join(timeout=2)
            self.reporter = None

    def wait_for_idle(self):
        # Detached Web workers are daemon threads. Joining only the scheduler
        # would otherwise let normal interpreter exit abandon accepted work.
        while True:
            with self.lock:
                if self.active == 0:
                    return
            time.sleep(0.05)

    def wait(self, stop, seconds):
        previous = stop.wake_condition
        stop.wake_condition = lambda: self.request() is not None
        try:
            return stop.wait(seconds)
        finally:
            stop.wake_condition = previous


def get_gate():
    from .config import get_settings
    path = (get_settings().poller.download_dir.parent / ".maintenance").absolute()
    with _gates_lock:
        if path not in _gates:
            _gates[path] = Gate(path)
        return _gates[path]


def tracked_thread(*, target, args=(), kwargs=None, **options):
    """Reserve at start, before a detached thread can outlive its admitting HTTP call."""
    ticket = None
    started = False
    start_lock = threading.Lock()

    def run(*a, **kw):
        with ticket.activate():
            return target(*a, **kw)

    run.__wrapped__ = target  # keep the dispatched callable introspectable
    thread = threading.Thread(target=run, args=args, kwargs=kwargs or {}, **options)
    original_start = thread.start

    def start():
        nonlocal ticket, started
        with start_lock:
            if started:
                raise RuntimeError("threads can only be started once")
            ticket = get_gate().reserve()
            started = True
            try:
                original_start()
            except BaseException:
                ticket.close()
                raise

    thread.start = start
    return thread


def tracked_submit(pool, target, *args, **kwargs):
    ticket = get_gate().reserve()

    def run():
        with ticket.activate():
            return target(*args, **kwargs)

    try:
        future = pool.submit(run)
        future.add_done_callback(lambda _: ticket.close())
        return future
    except BaseException:
        ticket.close()
        raise


class MaintenanceMiddleware:
    """Track full ASGI lifetime, including streaming and response background work."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            return await self.app(scope, receive, send)
        try:
            # Existing remote jobs must remain observable/cancellable while new
            # submissions pause. Authentication and tenancy still run downstream.
            path, method = scope.get("path", ""), scope.get("method", "GET")
            continuation = bool(re.fullmatch(
                r"/api/worker/v1/jobs/[0-9a-f]{32}(?:/artifacts/[^/]+)?", path)
                and method in ("GET", "HEAD")) or bool(re.fullmatch(
                r"/api/worker/v1/jobs/[0-9a-f]{32}/cancel", path) and method == "POST")
            ticket = get_gate().reserve(continuation=continuation)
        except MaintenancePaused:
            from starlette.responses import JSONResponse
            response = JSONResponse({"detail": "Service is draining for maintenance."},
                                    status_code=503, headers={"Retry-After": "30"})
            return await response(scope, receive, send)
        with ticket.activate():
            return await self.app(scope, receive, send)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-dir", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("begin")
    status = sub.add_parser("status")
    status.add_argument("--main-pid", type=int, required=True)
    status.add_argument("--voice-pid", type=int, required=True)
    resume = sub.add_parser("resume")
    resume.add_argument("--request-id", required=True)
    args = parser.parse_args()
    with directory(args.control_dir, create=args.action == "begin") as fd:
        lock = os.open(".control.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=fd)
        try:
            info = os.fstat(lock)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("unsafe maintenance control lock")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.action == "begin":
                result = {"request_id": uuid.uuid4().hex,
                          "started_at_utc": datetime.now(UTC).isoformat()}
                write_json(fd, "request.json", result, exclusive=True)
            else:
                request = read_json(fd, "request.json")
                if not re.fullmatch("[0-9a-f]{32}", request.get("request_id", "")):
                    raise ValueError("invalid maintenance request")
                if args.action == "resume":
                    if request.get("request_id") != args.request_id:
                        raise ValueError("maintenance request mismatch; preserved")
                    write_json(fd, "resume.json", {"request_id": args.request_id,
                               "resumed_at_utc": datetime.now(UTC).isoformat()})
                    os.unlink("request.json", dir_fd=fd)
                    os.fsync(fd)
                    result = {"resumed": True, "request_id": args.request_id}
                else:
                    result = {"request_id": request["request_id"], "quiescent": True, "services": {}}
                    for role, pid in (("main", args.main_pid), ("voice", args.voice_pid)):
                        receipt = read_json(fd, role + ".json")
                        observed = datetime.fromisoformat(receipt["observed_at_utc"])
                        age = (datetime.now(UTC) - observed).total_seconds()
                        valid = (pid > 0 and receipt.get("pid") == pid and receipt.get("role") == role
                                 and receipt.get("request_id") == request["request_id"]
                                 and receipt.get("process_birth") == process_birth(pid)
                                 and 0 <= age <= 2 and receipt.get("quiescent") is True
                                 and type(receipt.get("active")) is int and receipt["active"] == 0
                                 and receipt.get("serving") is True)
                        result["services"][role] = {"pid": pid, "active": receipt.get("active"),
                                                   "current_and_quiescent": valid}
                        result["quiescent"] &= valid
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("quiescent", True) else 2
        finally:
            os.close(lock)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("Maintenance precondition unconfirmed; no service action performed.") from None
