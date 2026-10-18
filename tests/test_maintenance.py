"""Private gate/drain, accepted-work handoff and isolated crash/resume tests."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from localplaud import maintenance as m


@pytest.fixture(autouse=True)
def restore_settings_cache(monkeypatch):
    yield
    # Settings are cached independently of monkeypatch's environment restoration.
    monkeypatch.undo()
    from localplaud.config import get_settings
    get_settings(reload=True)


def begin(path):
    with m.directory(path, create=True) as fd:
        m.write_json(fd, "request.json", {"request_id": "a" * 32}, exclusive=True)


@pytest.fixture
def gate(tmp_path, monkeypatch):
    g = m.Gate(tmp_path / ".maintenance")
    g.serving = True
    from localplaud.worker import daemon
    monkeypatch.setattr(m, "get_gate", lambda: g)
    monkeypatch.setattr(daemon, "get_gate", lambda: g)
    yield g
    g.close()


def test_absent_gate_preserves_normal_admission_and_pause_survives_new_instance(gate):
    with gate.activity():
        assert gate.snapshot()["active"] == 1
    assert gate.snapshot()["active"] == 0
    assert not gate.path.exists()  # opt-in: no control files created by normal work
    begin(gate.path)
    replacement = m.Gate(gate.path)
    with pytest.raises(m.MaintenancePaused):
        replacement.reserve()
    assert gate.snapshot()["quiescent"] is True


def test_existing_http_hands_off_detached_work_after_pause_without_zero_count_gap(gate):
    entered, finish = threading.Event(), threading.Event()
    with gate.activity():
        begin(gate.path)
        worker = m.tracked_thread(target=lambda: (entered.set(), finish.wait(3)))
        worker.start()
        assert entered.wait(1)
    assert gate.snapshot()["active"] == 1
    assert not gate.snapshot()["quiescent"]
    with pytest.raises(m.MaintenancePaused):
        gate.reserve()
    finish.set()
    worker.join(2)
    assert gate.snapshot()["quiescent"]


def test_thread_double_start_and_start_failure_do_not_leak_or_underflow(gate, monkeypatch):
    finish = threading.Event()
    thread = m.tracked_thread(target=finish.wait, args=(2,))
    thread.start()
    with pytest.raises(RuntimeError):
        thread.start()
    finish.set()
    thread.join(2)
    assert gate.active == 0

    class Broken:
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError("synthetic start failure")
    monkeypatch.setattr(threading, "Thread", Broken)
    with pytest.raises(RuntimeError):
        m.tracked_thread(target=lambda: None).start()
    assert gate.active == 0


def test_cancelled_pending_future_and_competing_ticket_close_are_exactly_once(gate):
    finish = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = m.tracked_submit(pool, finish.wait, 3)
        pending = m.tracked_submit(pool, lambda: pytest.fail("cancelled work ran"))
        assert pending.cancel()
        assert gate.active == 1
        finish.set()
        running.result(2)
    ticket = gate.reserve()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: ticket.close(), range(100)))
    assert gate.active == 0


def test_corrupt_oversized_symlink_and_foreign_mode_fail_closed(gate, tmp_path):
    begin(gate.path)
    request = gate.path / "request.json"
    for body in (b"{}", b"x" * 5000):
        request.write_bytes(body)
        with pytest.raises(m.MaintenancePaused): gate.reserve()
    request.unlink()
    target = tmp_path / "other"
    target.write_text('{"request_id":"' + 'a' * 32 + '"}')
    request.symlink_to(target)
    with pytest.raises(m.MaintenancePaused): gate.reserve()
    request.unlink()
    request.write_text('{"request_id":"' + 'a' * 32 + '"}')
    request.chmod(0o644)
    with pytest.raises(m.MaintenancePaused): gate.reserve()
    assert target.read_text().startswith('{"request_id"')


def test_asgi_full_stream_and_background_lifetime_are_tracked_and_health_stays_open(gate):
    entered = asyncio.Event()
    finish = asyncio.Event()
    messages = []

    async def application(scope, receive, send):
        if scope["path"] != "/healthz":
            entered.set()
            await finish.wait()  # stands for streaming and response background task
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message): messages.append(message)
    async def receive(): return {"type": "http.request", "body": b""}
    async def exercise():
        app = m.MaintenanceMiddleware(application)
        existing = asyncio.create_task(app({"type": "http", "path": "/api/work"}, receive, send))
        await entered.wait()
        begin(gate.path)
        assert gate.active == 1 and not gate.snapshot()["quiescent"]
        await app({"type": "http", "path": "/api/new"}, receive, send)
        assert messages[-2]["status"] == 503
        await app({"type": "http", "path": "/healthz"}, receive, send)
        assert messages[-2]["status"] == 200
        finish.set()
        await existing
        assert gate.snapshot()["quiescent"]
    asyncio.run(exercise())


def test_cli_status_binds_fresh_process_request_and_abort_resume(gate, monkeypatch, capsys):
    def cli(*args):
        monkeypatch.setattr(sys, "argv", ["maintenance", "--control-dir", str(gate.path), *args])
        rc = m.main()
        return rc, json.loads(capsys.readouterr().out)
    rc, request = cli("begin")
    assert rc == 0
    value = gate.snapshot() | {"process_birth": m.process_birth(os.getpid())}
    with m.directory(gate.path) as fd:
        for role in ("main", "voice"):
            m.write_json(fd, role + ".json", value | {"role": role})
    rc, status = cli("status", "--main-pid", str(os.getpid()), "--voice-pid", str(os.getpid()))
    assert rc == 0 and status["quiescent"] is True
    with m.directory(gate.path) as fd:
        m.write_json(fd, "voice.json", value | {"role": "voice", "process_birth": "stale birth"})
    assert cli("status", "--main-pid", str(os.getpid()), "--voice-pid", str(os.getpid()))[0] == 2
    with pytest.raises(ValueError): cli("resume", "--request-id", "b" * 32)
    assert gate.request() is not None
    assert cli("resume", "--request-id", request["request_id"])[1]["resumed"]
    with gate.activity(): pass


def test_voice_admitted_pass_drains_before_next_pass_and_request_persists(tmp_path):
    from tests.test_service_signals import FIXTURE, await_event, events
    root = tmp_path
    env = dict(os.environ, LOCALPLAUD_POLLER__DOWNLOAD_DIR=str(root / "audio"))
    child = subprocess.Popen([sys.executable, str(FIXTURE), "watch-active", str(root)],
                             cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
    try:
        await_event(child, root, "child-started")
        control = root / ".maintenance"
        begin(control)
        until = time.monotonic() + 3
        while True:
            with m.directory(control) as fd:
                try: receipt = m.read_json(fd, "voice.json")
                except FileNotFoundError: receipt = None
            if receipt: break
            assert time.monotonic() < until
            time.sleep(0.02)
        assert receipt["active"] == 1 and not receipt["quiescent"]
        (root / "release-child").touch()
        until = time.monotonic() + 3
        while True:
            with m.directory(control) as fd: receipt = m.read_json(fd, "voice.json")
            if receipt["quiescent"]: break
            assert time.monotonic() < until
            time.sleep(0.02)
        assert events(root).count("child-admitted") == 1
        assert (control / "request.json").exists()
    finally:
        (root / "release-child").touch()
        child.send_signal(2)
        stdout, stderr = child.communicate(timeout=5)
        assert child.returncode == 0, (stdout, stderr)


def setup_pipeline(monkeypatch, tmp_path):
    from tests.test_pipeline_resume import _reset_db, _install_fakes
    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    _reset_db(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    settings = get_settings(reload=True)
    init_db(reconcile_index=False)
    for name in ("first", "pending"):
        audio = tmp_path / (name + ".wav")
        audio.write_bytes(b"synthetic original retained")
        with session_scope() as db:
            db.add(PlaudFile(id=name, filename=name, origin="local",
                            status=FileStatus.downloaded, audio_path=str(audio)))
    counters = dict(asr=0, sum=0, mm=0, emb=0)
    _install_fakes(monkeypatch, counters)
    return settings, counters


def test_daemon_real_pipeline_longer_than60_drains_and_leaves_pending_durable(gate, monkeypatch, tmp_path):
    from localplaud import cli
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.worker import pipeline
    from localplaud.worker.daemon import DaemonJobs
    settings, counters = setup_pipeline(monkeypatch, tmp_path)
    settings.pipeline.concurrency = 1
    # Queue orders newest IDs first; pin the admitted recording without changing
    # the actual queue runner or durable selection behavior.
    with session_scope() as db:
        db.get(PlaudFile, "first").start_time_ms = 2000
        db.get(PlaudFile, "pending").start_time_ms = 1000
    entered, finished = threading.Event(), threading.Event()
    original = pipeline.transcribe.run_asr
    started = []

    def long_asr(*args, **kwargs):
        started.append(time.monotonic())
        entered.set()
        finished.wait(61.5)  # fake provider only; no audio decoding/model/network
        return original(*args, **kwargs)
    monkeypatch.setattr(pipeline.transcribe, "run_asr", long_asr)
    monkeypatch.setattr(cli, "process_automatic_pending", lambda *a, **k: pipeline.process_pending(settings))
    jobs = DaemonJobs(settings, "synthetic-daemon", object())
    thread = threading.Thread(target=jobs.work)
    thread.start()
    try:
        assert entered.wait(5)
        begin(gate.path)
        assert jobs.work() is None
        time.sleep(60.1)
        assert gate.active >= 1 and not gate.snapshot()["quiescent"]
        thread.join(10)
        assert not thread.is_alive() and time.monotonic() - started[0] > 60
        assert gate.snapshot()["quiescent"]
        with session_scope() as db:
            first, pending = db.get(PlaudFile, "first"), db.get(PlaudFile, "pending")
            assert first.status == FileStatus.done and first.local_transcript is not None
            assert pending.status == FileStatus.downloaded and pending.processing_token is None
        assert counters["asr"] == 1
        assert (tmp_path / "first.wav").read_bytes() == b"synthetic original retained"
    finally:
        finished.set()
        thread.join(5)
        jobs.shutdown()


def test_killed_stage_recovers_prior_owner_without_repeating_completed_asr(tmp_path, monkeypatch):
    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.poller.poll import reset_inflight
    from localplaud.worker import pipeline
    settings, counters = setup_pipeline(monkeypatch, tmp_path)
    ready = tmp_path / "stage-admitted"
    source = '''import time,sys,pytest
from pathlib import Path
from tests.test_pipeline_resume import _install_fakes
from localplaud.worker import pipeline
from localplaud.worker.claims import processing_owner
p=pytest.MonkeyPatch()
_install_fakes(p,dict(asr=0,sum=0,mm=0,emb=0))
def interrupted(*a,**k):
 Path(sys.argv[1]).touch()
 while True: time.sleep(0.1)
p.setattr(pipeline.summarize,'summarize',interrupted)
with processing_owner('synthetic-crashed-owner'): pipeline.process_file('first')
'''
    child = subprocess.Popen([sys.executable, "-c", source, str(ready)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        until = time.monotonic() + 10
        while not ready.exists():
            assert child.poll() is None, child.communicate(timeout=1)
            assert time.monotonic() < until
            time.sleep(0.02)
        child.kill()  # exact disposable fixture process; never a service PID
        child.communicate(timeout=3)
    finally:
        if child.poll() is None: child.kill(); child.wait(timeout=3)
    control = tmp_path / ".maintenance"
    begin(control)
    paused = m.Gate(control)
    assert reset_inflight(force=True, previous_owner="synthetic-crashed-owner") == 1
    with session_scope() as db:
        row = db.get(PlaudFile, "first")
        transcript_id = row.local_transcript.id
        assert row.processing_token is None and row.status == FileStatus.downloaded
        assert next(r for r in row.stage_runs if r.stage == StageName.transcribe).status == StageStatus.completed
        assert next(r for r in row.stage_runs if r.stage == StageName.summarize).status == StageStatus.failed
    with pytest.raises(m.MaintenancePaused): paused.reserve()
    with m.directory(control) as fd: os.unlink("request.json", dir_fd=fd)
    with paused.activity(): pipeline.process_file("first", get_settings())
    assert counters["asr"] == 0 and counters["sum"] == 1
    with session_scope() as db:
        row = db.get(PlaudFile, "first")
        assert row.status == FileStatus.done and row.local_transcript.id == transcript_id
        assert db.get(PlaudFile, "pending").status == FileStatus.downloaded
    assert (tmp_path / "first.wav").read_bytes() == b"synthetic original retained"


def test_remote_admitted_job_finishes_poll_download_cancel_and_owner_refresh_under_pause(gate, monkeypatch, tmp_path):
    from tests.test_remote_worker import _client, _request
    from localplaud.db.models import RemoteJob
    from localplaud.db.session import session_scope
    from localplaud.poller.poll import register_daemon_owner, refresh_daemon_owner, release_daemon_owner
    from localplaud.remote import server
    from localplaud.remote.protocol import JobStatus
    from sqlalchemy import select

    client = _client(monkeypatch, tmp_path)
    gate.start_reporting("main")
    headers = {"Authorization": "Bearer worker-secret"}
    entered, finish = threading.Event(), threading.Event()
    calls, responses = [], []
    def execute(request):
        calls.append(request.idempotency_key)
        entered.set()
        assert finish.wait(5)
        return [server._artifact("result.json", "application/json", b"completed")]
    monkeypatch.setattr(server, "_execute", execute)
    request = threading.Thread(target=lambda: responses.append(client.post(
        "/api/worker/v1/jobs", json=_request("accepted"), headers=headers)))
    request.start()
    try:
        assert entered.wait(3)
        with session_scope() as db:
            accepted_id = db.scalar(select(RemoteJob.id).where(RemoteJob.idempotency_key == "accepted"))
            for name in ("queued", "cancel"):
                db.add(RemoteJob(id=("b" if name == "queued" else "c") * 32,
                    idempotency_key=name, stage="summarize", model="test", status=JobStatus.queued,
                    input_manifest=_request(name), options={}))
        owner, _ = register_daemon_owner()
        begin(gate.path)
        assert refresh_daemon_owner(owner)  # Existing epoch/lease stays live while draining.
        assert client.get("/healthz").status_code == 200
        assert client.post("/api/worker/v1/jobs", json=_request("rejected"), headers=headers).status_code == 503
        url = "/api/worker/v1/jobs/" + accepted_id
        assert client.get(url).status_code == 401  # Exemption never bypasses worker auth.
        assert client.get(url, headers=headers).json()["status"] == "running"
        assert client.post("/api/worker/v1/jobs/" + "c" * 32 + "/cancel", headers=headers).status_code == 200
        assert server.execute_job("b" * 32) is False
        with session_scope() as db:
            assert db.get(RemoteJob, "b" * 32).status == JobStatus.queued
            assert db.get(RemoteJob, "b" * 32).input_manifest["inputs"][0]["value"] == {"segments": []}
            assert db.get(RemoteJob, "c" * 32).input_manifest["input_payloads_released"]
        finish.set()
        request.join(3)
        assert not request.is_alive() and responses[0].status_code == 200
        assert client.get(url, headers=headers).json()["status"] == "succeeded"
        assert client.get(url + "/artifacts/result.json", headers=headers).content == b"completed"
        assert gate.snapshot()["quiescent"]
        assert calls == ["accepted"]
        monkeypatch.setattr(sys, "argv", ["maintenance", "--control-dir", str(gate.path),
                                        "resume", "--request-id", "a" * 32])
        assert m.main() == 0
        until = time.monotonic() + 3
        while calls != ["accepted", "queued"]:
            assert time.monotonic() < until
            time.sleep(0.02)
        assert release_daemon_owner(owner)
    finally:
        finish.set()
        request.join(5)


@pytest.mark.parametrize("resume_before_restart", [False, True])
def test_standalone_restart_resumes_only_explicitly_deferred_jobs_with_auto_off(gate, monkeypatch, tmp_path, resume_before_restart):
    from tests.test_remote_worker import _client, _request
    from localplaud.db.models import RemoteJob
    from localplaud.db.session import session_scope
    from localplaud.preferences import get_workspace_preferences, save_workspace_preferences
    from localplaud.remote import server
    from localplaud.remote.protocol import JobStatus
    client = _client(monkeypatch, tmp_path)
    with session_scope() as db:
        save_workspace_preferences(db, {"auto_process_new_recordings": False})
        assert get_workspace_preferences(db)["auto_process_new_recordings"] is False
        for job_id, key in (("d" * 32, "deferred"), ("e" * 32, "historical")):
            db.add(RemoteJob(id=job_id, idempotency_key=key, stage="summarize", model="test",
                            status=JobStatus.queued, input_manifest=_request(key), options={}))
    begin(gate.path)
    assert server.execute_job("d" * 32) is False  # Real paused execution writes durable marker.
    assert not (gate.path / "main.json").exists()
    def resume():
        monkeypatch.setattr(sys, "argv", ["maintenance", "--control-dir", str(gate.path),
                                        "resume", "--request-id", "a" * 32])
        assert m.main() == 0
    if resume_before_restart:
        resume()
    replacement = m.Gate(gate.path)  # Fresh process owns no in-memory pause history.
    monkeypatch.setattr(m, "get_gate", lambda: replacement)
    calls = []
    monkeypatch.setattr(server, "_execute", lambda request: (
        calls.append(request.idempotency_key) or
        [server._artifact("result.json", "application/json", b"done")]))
    original_resume = server.resume_deferred_jobs
    attempts = []
    def scan_fail_once():
        attempts.append(True)
        if len(attempts) == 1:
            raise server.DeferredScanUnavailable("synthetic read unavailable")
        return original_resume()
    monkeypatch.setattr(server, "resume_deferred_jobs", scan_fail_once)
    from localplaud.api.app import app
    monkeypatch.setattr(app.state, "managed_daemon", False, raising=False)
    with client:
        assert replacement.reporter and replacement.serving
        if not resume_before_restart:
            time.sleep(0.3)
            assert calls == []
            resume()
        until = time.monotonic() + 3
        while calls != ["deferred"]:
            assert time.monotonic() < until
            time.sleep(0.02)
        # The background request may have reached the provider but not committed yet.
        while replacement.active:
            assert time.monotonic() < until
            time.sleep(0.02)
        with session_scope() as db:
            assert db.get(RemoteJob, "d" * 32).status == JobStatus.succeeded
            assert db.get(RemoteJob, "e" * 32).status == JobStatus.queued
            assert db.get(RemoteJob, "e" * 32).input_manifest["inputs"][0]["value"] == {"segments": []}
    assert replacement.reporter is None
    assert len(attempts) >= 2
