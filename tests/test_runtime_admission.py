"""Shared durable admission using fake recordings and isolated SQLite databases."""

import importlib
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from localplaud.db.models import (
    ExecutionProfile,
    FileStatus,
    PlaudFile,
    StageName,
    StageRun,
    StageStatus,
    Transcript,
    Workspace,
)
from localplaud.db.session import session_scope
from localplaud.db.tenancy import workspace_scope
from localplaud.worker import pipeline
from localplaud.worker.claims import processing_claim, processing_owner


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    import localplaud.db.session as database
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'runtime.db'}")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONCURRENCY", "2")
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_Session", None)
    settings = get_settings(reload=True)
    database.init_db(reconcile_index=False)
    with session_scope() as session:
        session.add(Workspace(id=2, name="synthetic member"))
    for workspace_id, ids in ((1, ("one", "two", "three")), (2, ("other",))):
        with workspace_scope(workspace_id), session_scope() as session:
            for file_id in ids:
                audio = tmp_path / f"{file_id}.wav"
                audio.write_bytes(b"fake audio, never decoded")
                session.add(PlaudFile(id=file_id, status=FileStatus.done, audio_path=str(audio)))
                session.add(Transcript(file_id=file_id, provider="fixture", source="local", text="fixture"))
                session.add(StageRun(
                    file_id=file_id, stage=StageName.transcribe,
                    status=StageStatus.completed, detail={"fixture": True},
                ))
    yield settings
    database.get_engine().dispose()


def test_competing_distinct_recordings_reserve_only_host_capacity(runtime):
    barrier = threading.Barrier(3)

    def claim(file_id):
        barrier.wait(timeout=3)
        try:
            return file_id, pipeline.claim_processing_work(file_id, require_audio=False)
        except pipeline.PipelineCapacityBusy:
            return file_id, None

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(claim, ("one", "two", "three")))
    admitted = [(file_id, token) for file_id, token in results if token]
    assert len(admitted) == 2
    rejected = next(file_id for file_id, token in results if token is None)
    with session_scope() as session:
        row = session.get(PlaudFile, rejected)
        assert row.status == FileStatus.done and row.processing_token is None
        assert row.stage_runs[0].detail == {"fixture": True}
    pipeline.release_processing_claim(*admitted[0], status=FileStatus.done)
    replacement = pipeline.claim_processing_work(rejected, require_audio=False)
    pipeline.release_processing_claim(rejected, replacement)
    pipeline.release_processing_claim(*admitted[1])


def test_independent_processes_share_the_durable_limit(runtime):
    command = (
        "import sys; from localplaud.worker.pipeline import claim_processing_work, PipelineCapacityBusy; "
        "\ntry: claim_processing_work(sys.argv[1], require_audio=False)"
        "\nexcept PipelineCapacityBusy: sys.exit(3)"
    )
    children = [subprocess.Popen(
        [sys.executable, "-c", command, file_id], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) for file_id in ("one", "two", "three")]
    try:
        codes = []
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            assert not stdout
            assert not stderr
            codes.append(child.returncode)
        assert sorted(codes) == [0, 0, 3]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)


def test_capacity_counts_other_workspaces_without_exposing_their_rows(runtime):
    one = pipeline.claim_processing_work("one", require_audio=False)
    with workspace_scope(2):
        other = pipeline.claim_processing_work("other", require_audio=False)
    with pytest.raises(pipeline.PipelineCapacityBusy):
        pipeline.claim_processing_work("two", require_audio=False)
    with session_scope() as session:
        assert session.get(PlaudFile, "other") is None
    with workspace_scope(2):
        pipeline.release_processing_claim("other", other)
    two = pipeline.claim_processing_work("two", require_audio=False)
    pipeline.release_processing_claim("one", one)
    pipeline.release_processing_claim("two", two)


def test_expired_and_prior_daemon_claims_recover_slots(runtime):
    from localplaud.poller.poll import reset_inflight

    with processing_owner("previous-daemon"):
        pipeline.claim_processing_work("one", require_audio=False)
    pipeline.claim_processing_work("two", require_audio=False)
    with session_scope() as session:
        session.get(PlaudFile, "two").processing_lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assert reset_inflight(force=True, previous_owner="previous-daemon") == 2
    with session_scope() as session:
        for file_id in ("one", "two"):
            row = session.get(PlaudFile, file_id)
            assert row.processing_token is None and row.status == FileStatus.downloaded
    pipeline.claim_processing_work("three", require_audio=False)
    pipeline.claim_processing_work("one", require_audio=False)


@pytest.mark.parametrize("route", ["reprocess", "generate_recording_notes"])
def test_manual_routes_reject_full_capacity_before_mutating_artifacts(runtime, monkeypatch, route):
    api = importlib.import_module("localplaud.api.app")
    pipeline.claim_processing_work("one", require_audio=False)
    pipeline.claim_processing_work("two", require_audio=False)
    monkeypatch.setattr(threading, "Thread", lambda **_: pytest.fail("must not start a thread"))
    response = getattr(api, route)("three", **({"profile_id": None} if route != "reprocess" else {}))
    assert response.status_code == 409
    assert b"capacity is full" in response.body
    with session_scope() as session:
        row = session.get(PlaudFile, "three")
        assert row.status == FileStatus.done and row.processing_token is None
        assert len(row.stage_runs) == 1
        assert row.stage_runs[0].status == StageStatus.completed


@pytest.mark.parametrize("route", ["reprocess", "generate_recording_notes"])
def test_thread_start_failure_releases_capacity(runtime, monkeypatch, route):
    api = importlib.import_module("localplaud.api.app")

    class BrokenThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("synthetic thread failure")

    monkeypatch.setattr(threading, "Thread", BrokenThread)
    response = getattr(api, route)("one", **({"profile_id": None} if route != "reprocess" else {}))
    assert response.status_code == 503
    with session_scope() as session:
        row = session.get(PlaudFile, "one")
        assert row.processing_token is None and row.status == FileStatus.done
    pipeline.claim_processing_work("two", require_audio=False)
    pipeline.claim_processing_work("three", require_audio=False)


def test_route_handoff_preserves_force_profile_and_speech_scope(runtime, monkeypatch):
    api = importlib.import_module("localplaud.api.app")
    started = []

    class DeferredThread:
        def __init__(self, **kwargs):
            self.options = kwargs

        def start(self):
            started.append(self.options)

    monkeypatch.setattr(threading, "Thread", DeferredThread)
    monkeypatch.setattr("localplaud.poller.poll.current_daemon_owner", lambda: "fixture-owner")
    with session_scope() as session:
        profile_id = session.scalar(select(ExecutionProfile.id))
    assert api.reprocess("one", force=True).status_code == 200
    assert api.generate_recording_notes("two", profile_id=profile_id).status_code == 200
    assert started[0]["kwargs"]["force"] is True
    assert started[1]["kwargs"]["profile_id"] == profile_id
    with session_scope() as session:
        row = session.get(PlaudFile, "two")
        assert row.processing_token.startswith("daemon:fixture-owner:")
        speech = next(run for run in row.stage_runs if run.stage == StageName.transcribe)
        assert speech.status == StageStatus.completed and speech.detail == {"fixture": True}
        for run in row.stage_runs:
            if run.stage != StageName.transcribe:
                assert run.detail["derived_only"] is True


@pytest.mark.parametrize("ignore_term", [False, True])
def test_lost_recording_lease_cancels_nested_vad_and_preserves_new_owner(runtime, tmp_path, ignore_term):
    from localplaud.subprocesses import run_process
    from tests.runtime_process_fixtures import assert_stopped, nested_vad_process

    token = pipeline.claim_processing_work("one", require_audio=False)
    ready = tmp_path / "descendant.pid"

    def displace():
        import time

        deadline = time.monotonic() + 3
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        with session_scope() as session:
            session.get(PlaudFile, "one").processing_token = "replacement"

    with nested_vad_process(tmp_path, ignore_term=ignore_term) as (command, paths):
        with ThreadPoolExecutor(max_workers=1) as pool:
            displaced = pool.submit(displace)
            with processing_claim("one", token), pytest.raises(pipeline.PipelineAlreadyRunning):
                run_process(command, timeout=5)
            displaced.result(timeout=1)
        assert_stopped(paths)
    pipeline.release_processing_claim("one", token)
    with session_scope() as session:
        assert session.get(PlaudFile, "one").processing_token == "replacement"


def test_hung_subprocess_records_failure_releases_slot_and_allows_next_recording(runtime, monkeypatch):
    from localplaud.subprocesses import ProcessDeadlineExceeded, run_process

    runtime.pipeline.concurrency = 1
    runtime.store.evict_plaud_audio_after_processing = False

    def hung_stage(file_id, **kwargs):
        pipeline._begin_stage(file_id, StageName.transcribe)
        try:
            run_process([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.2)
        except ProcessDeadlineExceeded as exc:
            pipeline._fail_stage(file_id, StageName.transcribe, exc)
            raise

    monkeypatch.setattr(pipeline, "_process_file_claimed", hung_stage)
    with pytest.raises(ProcessDeadlineExceeded):
        pipeline.process_file("one", runtime)
    with session_scope() as session:
        row = session.get(PlaudFile, "one")
        assert row.processing_token is None
        assert row.status == FileStatus.error and row.pipeline_next_retry_at is not None
        assert row.stage_runs[0].status == StageStatus.failed
    next_token = pipeline.claim_processing_work("two", require_audio=False)
    pipeline.release_processing_claim("two", next_token)
