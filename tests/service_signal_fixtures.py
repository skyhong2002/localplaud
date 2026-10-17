"""No listeners, models, providers, application lifespan or production database.

Run only under tests/runtime_guard. Every Python child inherits startup isolation.
Uvicorn's installed capture_signals/handle_exit are real; its HTTP loop is synthetic.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

assert getattr(sys, "_localplaud_runtime_guard", False)


def deny_sockets(event, args):
    if event.startswith("socket."):
        raise RuntimeError("signal fixture must not use any sockets")


sys.addaudithook(deny_sockets)
mode, root_arg = sys.argv[1:3]
root = Path(root_arg)


def emit(event):
    with (root / "events").open("a") as stream:
        stream.write(event + "\n")


def until(name):
    deadline = time.monotonic() + 15
    while not (root / name).exists():
        assert time.monotonic() < deadline, name
        time.sleep(0.01)


def stub(name, **members):
    module = types.ModuleType(name)
    module.__dict__.update(members)
    sys.modules[name] = module


previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

if mode.startswith("run"):
    import uvicorn

    from localplaud import service_signals

    class ObservedCombinedSignals(service_signals.ServiceSignals):
        def request_stop(self, signum=None, frame=None):
            if signum is not None:
                emit("outer-signal-" + str(signum))
            super().request_stop(signum, frame)

    service_signals.ServiceSignals = ObservedCombinedSignals

    emit("uvicorn-" + uvicorn.__version__)

    if mode == "run-startup-signal":
        config = uvicorn.Config

        def delayed_config(*args, **kwargs):
            emit("config-started")
            until("release-config")
            return config(*args, **kwargs)

        uvicorn.Config = delayed_config

    class Scheduler:
        running = False

        def start(self):
            self.running = True

        def shutdown(self, *, wait):
            assert wait and jobs.stopped.is_set()
            emit("scheduler-shutdown")
            self.running = False

    class Jobs:
        def __init__(self, *args):
            global jobs
            jobs = self
            self.stopped = threading.Event()
            self.thread = None

        def install(self):
            if mode == "run-install-failure":
                raise RuntimeError("synthetic install failure")

            def worker():
                emit("worker-started")
                until("release-worker")
                emit("worker-finished")

            self.thread = threading.Thread(target=worker)
            self.thread.start()

        def shutdown(self):
            assert self.stopped.is_set()
            emit("jobs-shutdown")
            if self.thread is not None:
                self.thread.join(16)
                assert not self.thread.is_alive()
            if mode == "run-cleanup-failure":
                raise RuntimeError("synthetic cleanup failure")

    def register():
        emit("owner-registered")
        return "fixture-owner", "previous-owner"

    def release(owner):
        assert owner == "fixture-owner"
        emit("owner-released")

    def recover(**kwargs):
        if mode == "run-recovery-failure":
            raise RuntimeError("synthetic recovery failure")

    # Stub everything with state or IO before cli.run can import it.
    stub("apscheduler.schedulers.background", BackgroundScheduler=Scheduler)
    stub("localplaud.ask_threads", recover_ask_request_claims=lambda _: None)
    stub("localplaud.db.session", init_db=lambda **_: None)
    stub("localplaud.db.tenancy", system_scope=contextlib.nullcontext)
    stub(
        "localplaud.poller.poll",
        register_daemon_owner=register,
        release_daemon_owner=release,
        reset_inflight=recover,
    )
    stub("localplaud.providers.usage", recover_provider_dispatch_reservations=lambda _: None)
    stub("localplaud.worker.daemon", DaemonJobs=Jobs)
    stub("localplaud.api.app", app=types.SimpleNamespace(state=types.SimpleNamespace()))

    def synthetic_http(self):
        # Execute installed capture/replay behavior, without binding sockets or
        # loading the real ASGI app/lifespan. Do not replace handle_exit.
        with self.capture_signals():
            emit("ready")
            if mode in {"run-normal", "run-cleanup-failure", "run-serve-failure"}:
                if mode == "run-serve-failure":
                    raise RuntimeError("synthetic serve failure")
                return
            while not self.should_exit:
                time.sleep(0.01)
            assert jobs.stopped.is_set(), "dispatch must stop before HTTP shutdown"
            emit("dispatch-stopped")
            until("release-http")
            emit("http-shutdown-finished")

    uvicorn.Server.run = synthetic_http
    from localplaud import cli

    try:
        cli.run()
    except RuntimeError:
        assert mode.endswith("failure")
        emit("expected-failure")

elif mode == "voice-child":
    # A real disposable process with default TERM/INT policy, never a model.
    (root / "child-pid").write_text(str(os.getpid()))
    emit("child-started")
    until("release-child")
    (root / "receipt.json").write_text(json.dumps({"counts": {"recordings_extracted": 1}}))
    emit("child-finished")

elif mode.startswith("watch"):
    from localplaud import voice_service as service

    service.get_engine = lambda: None
    service.create_schema = lambda _: None

    def fingerprint(_):
        if mode == "watch-before-admission":
            emit("fingerprint-started")
            until("release-fingerprint")
        return "unchanged"

    service.library_fingerprint = fingerprint
    service.VoiceWorker = lambda *a: (_ for _ in ()).throw(AssertionError("real worker forbidden"))
    run = subprocess.run

    def synthetic_child(command, *, check, start_new_session):
        assert command[-1] == "--child-pass" and start_new_session
        emit("child-admitted")
        run(
            [sys.executable, __file__, "voice-child", str(root)],
            check=check,
            start_new_session=start_new_session,
        )

    service.subprocess.run = synthetic_child
    from localplaud.service_signals import ServiceSignals

    class ObservedSignals(ServiceSignals):
        def __init__(self):
            super().__init__()
            wait = self.wait
            self.waits = 0

            def observed_wait(timeout):
                self.waits += 1
                if mode == "watch-cached" and self.waits == 1:
                    return False
                emit("idle-" + str(timeout))
                return wait(timeout)

            self.wait = observed_wait

        def request_stop(self, *args):
            super().request_stop(*args)
            emit("stop-requested")

    service.ServiceSignals = ObservedSignals
    profile = root / "profile.json"
    profile.write_text(json.dumps({"enabled": mode != "watch-disabled", "poll_seconds": 300}))
    sys.argv = ["voice-service", "--profile", str(profile), "--watch"]
    if mode != "watch-cached":
        sys.argv.extend(["--limit", "1"])
    service.main()

else:
    raise AssertionError(mode)

assert {sig: signal.getsignal(sig) for sig in previous} == previous
emit("handlers-restored")
