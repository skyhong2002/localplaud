"""Verified cooperative TERM/INT with installed Uvicorn and synthetic children."""

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from localplaud.service_signals import ServiceSignals

pytestmark = pytest.mark.skipif(
    not getattr(sys, "_localplaud_runtime_guard", False),
    reason="requires interpreter-startup isolation: use tests/run_runtime_offline.sh",
)
FIXTURE = Path(__file__).with_name("service_signal_fixtures.py")


def events(root):
    path = root / "events"
    return path.read_text().splitlines() if path.exists() else []


def await_event(child, root, event):
    deadline = time.monotonic() + 8
    while event not in events(root):
        assert child.poll() is None, child.communicate(timeout=2)
        assert time.monotonic() < deadline, events(root)
        time.sleep(0.01)


@contextlib.contextmanager
def fixture(mode, root):
    child = subprocess.Popen(
        [sys.executable, str(FIXTURE), mode, str(root)],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        yield child
    finally:
        # Release synthetic work even after an assertion fails; never target a
        # host process/group or leave the isolated voice child running.
        for name in (
            "release-http",
            "release-worker",
            "release-child",
            "release-fingerprint",
            "release-config",
        ):
            (root / name).touch()
        try:
            child.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=2)


def assert_clean_exit(child, root):
    stdout, stderr = child.communicate(timeout=5)
    assert child.returncode == 0, (events(root), stdout, stderr)
    assert events(root)[-1] == "handlers-restored"


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_combined_signal_stops_dispatch_and_waits_worker_through_uvicorn_replay(tmp_path, sig):
    with fixture("run-signal", tmp_path) as child:
        await_event(child, tmp_path, "ready")
        await_event(child, tmp_path, "worker-started")
        child.send_signal(sig)
        await_event(child, tmp_path, "dispatch-stopped")
        # Repeated TERM while Uvicorn owns the handler is replayed too.
        child.send_signal(signal.SIGTERM)
        (tmp_path / "release-http").touch()
        await_event(child, tmp_path, "jobs-shutdown")
        assert "http-shutdown-finished" in events(tmp_path)
        assert "outer-signal-" + str(sig) in events(tmp_path), "Uvicorn must replay the signal"
        assert "worker-finished" not in events(tmp_path)
        # The cleanup must outlive the deployment's five-second grace period.
        child.send_signal(signal.SIGTERM)
        time.sleep(5.2)
        assert child.poll() is None
        assert "owner-released" not in events(tmp_path)
        child.send_signal(signal.SIGTERM)
        (tmp_path / "release-worker").touch()
        assert_clean_exit(child, tmp_path)
        rows = events(tmp_path)
        assert rows.count("owner-released") == rows.count("jobs-shutdown") == 1
        assert rows.index("worker-finished") < rows.index("owner-released")


def test_combined_signal_before_uvicorn_capture_is_retained(tmp_path):
    with fixture("run-startup-signal", tmp_path) as child:
        await_event(child, tmp_path, "config-started")
        child.send_signal(signal.SIGTERM)
        await_event(child, tmp_path, "outer-signal-15")
        (tmp_path / "release-config").touch()
        # Uvicorn must observe the stop without requiring another signal.
        await_event(child, tmp_path, "dispatch-stopped")
        (tmp_path / "release-http").touch()
        await_event(child, tmp_path, "jobs-shutdown")
        assert "worker-finished" not in events(tmp_path)
        (tmp_path / "release-worker").touch()
        assert_clean_exit(child, tmp_path)
        assert events(tmp_path).count("owner-released") == 1


@pytest.mark.parametrize(
    "mode",
    [
        "run-normal",
        "run-recovery-failure",
        "run-install-failure",
        "run-serve-failure",
        "run-cleanup-failure",
    ],
)
def test_combined_normal_and_failure_paths_release_owner_once(tmp_path, mode):
    (tmp_path / "release-worker").touch()
    with fixture(mode, tmp_path) as child:
        assert_clean_exit(child, tmp_path)
        assert events(tmp_path).count("owner-released") == 1
        if mode != "run-recovery-failure":
            assert events(tmp_path).count("jobs-shutdown") == 1


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_watch_group_signal_finishes_child_and_admits_no_next_pass(tmp_path, sig):
    with fixture("watch-active", tmp_path) as child:
        await_event(child, tmp_path, "child-started")
        child_pid = int((tmp_path / "child-pid").read_text())
        assert os.getpgid(child_pid) != os.getpgid(child.pid)
        os.killpg(child.pid, sig)
        await_event(child, tmp_path, "stop-requested")
        child.send_signal(signal.SIGTERM)
        time.sleep(0.15)
        assert child.poll() is None
        assert "child-finished" not in events(tmp_path)
        start = time.monotonic()
        (tmp_path / "release-child").touch()
        assert_clean_exit(child, tmp_path)
        assert time.monotonic() - start < 2
        assert events(tmp_path).count("child-admitted") == 1
        assert "child-finished" in events(tmp_path)


@pytest.mark.parametrize(
    "mode,idle",
    [
        ("watch-disabled", "idle-30"),
        ("watch-idle", "idle-300"),
        ("watch-cached", "idle-300"),
    ],
)
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_watch_idle_signal_wakes_promptly(tmp_path, mode, idle, sig):
    (tmp_path / "release-child").touch()
    with fixture(mode, tmp_path) as child:
        await_event(child, tmp_path, idle)
        start = time.monotonic()
        child.send_signal(sig)
        assert_clean_exit(child, tmp_path)
        assert time.monotonic() - start < 2
        assert events(tmp_path).count("child-admitted") == (mode != "watch-disabled")


def test_watch_signal_during_fingerprint_prevents_child_admission(tmp_path):
    with fixture("watch-before-admission", tmp_path) as child:
        await_event(child, tmp_path, "fingerprint-started")
        child.send_signal(signal.SIGTERM)
        await_event(child, tmp_path, "stop-requested")
        (tmp_path / "release-fingerprint").touch()
        assert_clean_exit(child, tmp_path)
        assert "child-admitted" not in events(tmp_path)


def test_scope_restores_custom_handlers_on_exception_and_has_no_thread_side_effects():
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def custom(*_):
        pass

    try:
        for sig in previous:
            signal.signal(sig, custom)
        with pytest.raises(RuntimeError, match="synthetic"):
            with ServiceSignals() as stop:
                signal.raise_signal(signal.SIGTERM)
                signal.raise_signal(signal.SIGINT)
                assert stop.requested
                raise RuntimeError("synthetic")
        assert all(signal.getsignal(sig) is custom for sig in previous)
        errors = []

        def in_thread():
            try:
                with ServiceSignals():
                    assert all(signal.getsignal(sig) is custom for sig in previous)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=in_thread)
        thread.start()
        thread.join(2)
        assert not thread.is_alive() and not errors
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def test_import_installs_no_handlers(tmp_path):
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import signal; "
            "before = [signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)]; "
            "import localplaud.service_signals, localplaud.cli, localplaud.voice_service; "
            "assert before == [signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)]",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert child.returncode == 0, child.stderr


def test_standalone_serve_preserves_uvicorn_command_path(monkeypatch):
    from unittest.mock import Mock

    import uvicorn

    from localplaud import cli
    from localplaud.config import Settings

    settings = Settings(_env_file=None)
    run = Mock()
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(uvicorn, "run", run)
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    cli.serve()
    run.assert_called_once_with(
        "localplaud.api.app:app",
        host=settings.api.host,
        port=settings.api.port,
        log_level="info",
    )
    assert {sig: signal.getsignal(sig) for sig in before} == before


@pytest.mark.parametrize("transferred_stop", [False, True])
@pytest.mark.parametrize("first", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("second", [signal.SIGTERM, signal.SIGINT])
def test_managed_repeated_signals_drain_installed_uvicorn_tasks_and_lifespan(
    monkeypatch, transferred_stop, first, second
):
    """Drive real shutdown/task/lifespan coroutines with a socket-free scheduler."""
    import asyncio
    import types

    import uvicorn
    from uvicorn.lifespan.on import LifespanOn

    from localplaud import cli
    from localplaud.config import Settings

    class Tick:
        def __init__(self, name):
            self.name = name

        def __await__(self):
            yield self.name

    async def sleep(_):
        await Tick("sleep")

    async def wait_for(coroutine, timeout):
        return await coroutine

    async def event_wait(event):
        while not event.is_set():
            await Tick("lifespan-wait")
        return True

    rows = []

    async def asgi(scope, receive, send):
        assert scope["type"] == "lifespan"
        assert await receive() == {"type": "lifespan.startup"}
        await send({"type": "lifespan.startup.complete"})
        await Tick("lifespan-idle")
        assert await receive() == {"type": "lifespan.shutdown"}
        rows.append("cleanup-started")
        await Tick("lifespan-cleanup")
        rows.append("cleanup-finished")
        await send({"type": "lifespan.shutdown.complete"})

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    monkeypatch.setattr(asyncio.Event, "wait", event_wait)
    monkeypatch.setitem(sys.modules, "localplaud.api.app", types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace())
    ))

    def run(server):
        server.servers = []
        server.lifespan = LifespanOn(types.SimpleNamespace(
            loaded=True, loaded_app=asgi, asgi_version="3.0", lifespan="on"
        ))
        lifespan = server.lifespan.main()
        shutdown = server.shutdown()
        server.lifespan.receive_queue.put_nowait({"type": "lifespan.startup"})
        server.server_state.tasks.add(object())
        try:
            assert lifespan.send(None) == "lifespan-idle"
            with server.capture_signals():
                if not transferred_stop:
                    signal.raise_signal(first)
                assert server.should_exit and stop.requested
                assert shutdown.send(None) == "sleep"
                signal.raise_signal(second)
                # Installed _wait_tasks_to_complete must still wait for the task.
                assert shutdown.send(None) == "sleep"
                assert server.server_state.tasks and not server.force_exit
                assert rows == []
                server.server_state.tasks.clear()
                assert shutdown.send(None) == "lifespan-wait"
                assert lifespan.send(None) == "lifespan-cleanup"
                signal.raise_signal(second)
                assert shutdown.send(None) == "lifespan-wait"
                assert rows == ["cleanup-started"] and not server.force_exit
                with pytest.raises(StopIteration):
                    lifespan.send(None)
                with pytest.raises(StopIteration):
                    shutdown.send(None)
                assert rows == ["cleanup-started", "cleanup-finished"]
                assert not server.lifespan.error_occurred
                assert server._captured_signals == captured
            assert replayed == list(reversed(captured))
        finally:
            shutdown.close()
            lifespan.close()

    class ObservedSignals(ServiceSignals):
        def request_stop(self, signum=None, frame=None):
            if signum is not None:
                replayed.append(signum)
            super().request_stop(signum, frame)

    replayed = []
    stop_calls = []
    captured = ([] if transferred_stop else [first]) + [second, second]
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    monkeypatch.setattr(uvicorn.Server, "run", run)
    with ObservedSignals(lambda: stop_calls.append("stop")) as stop:
        if transferred_stop:
            signal.raise_signal(first)
            replayed.clear()
        cli._serve(Settings(_env_file=None), managed_daemon=True, service_stop=stop)
        assert stop_calls == ["stop"]
    assert {sig: signal.getsignal(sig) for sig in before} == before
