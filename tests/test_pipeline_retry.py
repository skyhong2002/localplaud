"""Durable pipeline retries use bounded backoff and never starve fresh files."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


def _reset(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'retry.db'}")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONVERT", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__DIARIZE", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__POLISH", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__SUMMARIZE", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__MIND_MAP", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__INDEX", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__AUTO_PROCESS_UNTRANSCRIBED_ONLY", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__RETRY_BASE_SECONDS", "10")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__RETRY_MAX_SECONDS", "25")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    settings = get_settings(reload=True)
    from localplaud.db.session import init_db

    init_db()
    return settings


def test_retry_schedule_is_exponential_and_bounded(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile
    from localplaud.worker.pipeline import _schedule_pipeline_retry, reset_pipeline_retry

    row = PlaudFile(id="retry")
    before = datetime.now(UTC)
    _schedule_pipeline_retry(row, settings)
    assert row.pipeline_retry_count == 1
    assert before + timedelta(seconds=9) <= row.pipeline_next_retry_at
    _schedule_pipeline_retry(row, settings)
    assert row.pipeline_retry_count == 2
    assert row.pipeline_next_retry_at >= datetime.now(UTC) + timedelta(seconds=19)
    _schedule_pipeline_retry(row, settings)
    assert row.pipeline_retry_count == 3
    # Past the fast budget the recording keeps a slow, unattended cadence.
    slow = settings.pipeline.retry_exhausted_interval_seconds
    assert row.pipeline_next_retry_at >= datetime.now(UTC) + timedelta(seconds=slow - 1)
    settings.pipeline.retry_exhausted_interval_seconds = 0
    _schedule_pipeline_retry(row, settings)
    assert row.pipeline_next_retry_at is None
    reset_pipeline_retry(row)
    assert row.pipeline_retry_count == 0
    assert row.pipeline_next_retry_at is None and row.pipeline_last_failure_at is None


def test_pending_queue_prioritizes_fresh_and_only_due_retries(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id="fresh",
                    status=FileStatus.downloaded,
                    audio_path=str(audio),
                    start_time_ms=int(now.timestamp() * 1000),
                ),
                PlaudFile(
                    id="due",
                    status=FileStatus.error,
                    audio_path=str(audio),
                    start_time_ms=500,
                    pipeline_retry_count=1,
                    pipeline_next_retry_at=now - timedelta(seconds=1),
                ),
                PlaudFile(
                    id="legacy",
                    status=FileStatus.partial,
                    audio_path=str(audio),
                    start_time_ms=300,
                    pipeline_retry_count=0,
                    pipeline_last_failure_at=now - timedelta(seconds=2),
                ),
                PlaudFile(
                    id="future",
                    status=FileStatus.error,
                    audio_path=str(audio),
                    start_time_ms=900,
                    pipeline_retry_count=1,
                    pipeline_next_retry_at=now + timedelta(hours=1),
                ),
                PlaudFile(
                    id="exhausted",
                    status=FileStatus.error,
                    audio_path=str(audio),
                    start_time_ms=800,
                    pipeline_retry_count=3,
                ),
                PlaudFile(
                    id="no-audio",
                    status=FileStatus.error,
                    start_time_ms=1000,
                    pipeline_retry_count=0,
                ),
            ]
        )
    seen: list[str] = []

    def complete(file_id, *_args, **_kwargs):
        seen.append(file_id)
        with session_scope() as session:
            session.get(PlaudFile, file_id).status = FileStatus.done

    monkeypatch.setattr(pipeline, "process_file", complete)
    assert pipeline.process_pending(settings, limit=3) == 3
    assert seen == ["fresh", "due", "legacy"]


def test_fresh_download_is_not_starved_by_churning_retries(monkeypatch, tmp_path):
    """A due retry's timestamp is always ≈now, so ranking purely by event time
    let a failing-and-rescheduling backlog starve a new recording forever.
    Fresh downloads are finite (each processed file leaves the class), so they
    take absolute priority."""
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    audio = tmp_path / "queue.wav"
    audio.write_bytes(b"RIFF")
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id=f"churn-{index}",
                    status=FileStatus.partial,
                    audio_path=str(audio),
                    start_time_ms=int((now - timedelta(days=100 + index)).timestamp() * 1000),
                    pipeline_retry_count=1,
                    pipeline_next_retry_at=now - timedelta(seconds=index + 1),
                )
                for index in range(5)
            ]
            + [
                PlaudFile(
                    id="fresh-today",
                    status=FileStatus.downloaded,
                    audio_path=str(audio),
                    start_time_ms=int((now - timedelta(hours=8)).timestamp() * 1000),
                )
            ]
        )
    seen: list[str] = []
    monkeypatch.setattr(
        pipeline, "process_file", lambda file_id, *_args, **_kwargs: seen.append(file_id)
    )
    assert pipeline.process_pending(settings, limit=1) == 1
    assert seen == ["fresh-today"]


def test_new_downloads_keep_all_slots_ahead_of_retries(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    audio = tmp_path / "queue.wav"
    audio.write_bytes(b"RIFF")
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id=f"backlog-{index}",
                    status=FileStatus.downloaded,
                    audio_path=str(audio),
                    start_time_ms=int((now - timedelta(days=10 + index)).timestamp() * 1000),
                )
                for index in range(20)
            ]
            + [
                PlaudFile(
                    id="due-retry",
                    status=FileStatus.error,
                    audio_path=str(audio),
                    pipeline_retry_count=1,
                    pipeline_next_retry_at=now - timedelta(minutes=1),
                )
            ]
        )
    seen: list[str] = []
    monkeypatch.setattr(
        pipeline, "process_file", lambda file_id, *_args, **_kwargs: seen.append(file_id)
    )
    assert pipeline.process_pending(settings, limit=2) == 2
    assert seen == ["backlog-0", "backlog-1"]


def test_pending_batch_interleaves_one_full_retry_with_derived_work(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope

    audio = tmp_path / "interleaved.wav"
    audio.write_bytes(b"RIFF")
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id=f"full-{index}",
                    status=FileStatus.partial,
                    audio_path=str(audio),
                    pipeline_next_retry_at=now - timedelta(seconds=index + 1),
                )
                for index in range(3)
            ]
            + [
                PlaudFile(
                    id=f"derived-{index}",
                    status=FileStatus.partial,
                    stage_runs=[
                        StageRun(
                            stage=StageName.summarize,
                            status=StageStatus.pending,
                            detail={"derived_only": True},
                        )
                    ],
                )
                for index in range(3)
            ]
        )

    full: list[str] = []
    derived: list[str] = []
    monkeypatch.setattr(
        pipeline, "process_file", lambda file_id, *_args, **_kwargs: full.append(file_id)
    )
    monkeypatch.setattr(
        pipeline,
        "process_derived_artifacts",
        lambda file_id, *_args, **_kwargs: derived.append(file_id),
    )

    assert pipeline.process_pending(settings, limit=4) == 4
    assert len(full) == 1
    assert len(derived) == 3


def test_pending_batch_revalidates_retry_deadline_before_each_job(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    audio = tmp_path / "deferred.wav"
    audio.write_bytes(b"RIFF")
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id="first",
                    status=FileStatus.error,
                    audio_path=str(audio),
                    pipeline_retry_count=1,
                    pipeline_next_retry_at=now - timedelta(seconds=1),
                ),
                PlaudFile(
                    id="deferred",
                    status=FileStatus.error,
                    audio_path=str(audio),
                    pipeline_retry_count=1,
                    pipeline_next_retry_at=now - timedelta(seconds=2),
                ),
            ]
        )

    seen: list[str] = []

    def process(file_id, *_args, **_kwargs):
        seen.append(file_id)
        if file_id == "first":
            with session_scope() as session:
                session.get(PlaudFile, "deferred").pipeline_next_retry_at = datetime.now(
                    UTC
                ) + timedelta(hours=1)

    monkeypatch.setattr(pipeline, "process_file", process)
    assert pipeline.process_pending(settings, limit=2) == 1
    assert seen == ["first"]


def test_pipeline_failure_is_retried_then_success_clears_state(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.asr.base import Segment, Transcript
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    audio = tmp_path / "failure.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as session:
        session.add(PlaudFile(id="recover", status=FileStatus.downloaded, audio_path=str(audio)))
    monkeypatch.setattr(
        pipeline.transcribe,
        "run_asr",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("temporary ASR outage")),
    )
    assert pipeline.process_pending(settings) == 0
    with session_scope() as session:
        row = session.get(PlaudFile, "recover")
        assert row.status == FileStatus.error
        assert row.pipeline_retry_count == 1 and row.pipeline_next_retry_at is not None
        row.pipeline_next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(
        pipeline.transcribe,
        "run_asr",
        lambda *_args, **_kwargs: Transcript(
            segments=[Segment(text="recovered", start=0, end=1)],
            language="en",
            provider="fake",
        ),
    )
    assert pipeline.process_pending(settings) == 1
    with session_scope() as session:
        row = session.get(PlaudFile, "recover")
        assert row.status == FileStatus.done
        assert row.pipeline_retry_count == 0
        assert row.pipeline_next_retry_at is None and row.pipeline_last_failure_at is None


def test_retry_migration_is_idempotent(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from localplaud.db.migrations import migrate_pipeline_retry_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)"))
    assert set(migrate_pipeline_retry_schema(engine)) == {
        "plaud_files.pipeline_retry_count",
        "plaud_files.pipeline_next_retry_at",
        "plaud_files.pipeline_last_failure_at",
        "plaud_files.process_overlong",
    }
    assert {column["name"] for column in inspect(engine).get_columns("plaud_files")} >= {
        "pipeline_retry_count",
        "pipeline_next_retry_at",
        "pipeline_last_failure_at",
        "process_overlong",
    }
    assert migrate_pipeline_retry_schema(engine) == []


def test_processing_claim_migration_is_idempotent(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from localplaud.db.migrations import migrate_processing_claim_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-claim.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)"))
    assert set(migrate_processing_claim_schema(engine)) == {
        "plaud_files.processing_token",
        "plaud_files.processing_lease_until",
        "plaud_files.download_token",
        "plaud_files.download_lease_until",
    }
    assert migrate_processing_claim_schema(engine) == []
    columns = {column["name"] for column in inspect(engine).get_columns("plaud_files")}
    assert {
        "processing_token",
        "processing_lease_until",
        "download_token",
        "download_lease_until",
    } <= columns


def test_processing_claim_migration_backfills_one_fixed_legacy_download_lease(tmp_path):
    from sqlalchemy import create_engine, text

    from localplaud.db.migrations import migrate_processing_claim_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-download.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY, "
                "status VARCHAR(20), updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO plaud_files (id, status, updated_at) "
                "VALUES ('active', 'downloading', CURRENT_TIMESTAMP)"
            )
        )
    migrate_processing_claim_schema(engine)
    with engine.begin() as connection:
        first = connection.execute(
            text("SELECT download_lease_until FROM plaud_files WHERE id = 'active'")
        ).scalar_one()
    assert first is not None
    assert migrate_processing_claim_schema(engine) == []
    with engine.begin() as connection:
        second = connection.execute(
            text("SELECT download_lease_until FROM plaud_files WHERE id = 'active'")
        ).scalar_one()
    assert second == first


def test_manual_resume_resets_retry_budget(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient

    import localplaud.worker.pipeline as pipeline
    from localplaud.api.app import app
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    audio = tmp_path / "manual.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="manual",
                status=FileStatus.error,
                audio_path=str(audio),
                pipeline_retry_count=3,
                pipeline_next_retry_at=None,
                pipeline_last_failure_at=datetime.now(UTC),
            )
        )
    monkeypatch.setattr(pipeline, "process_file", lambda *_args, **_kwargs: None)
    response = TestClient(app).post("/file/manual/reprocess")
    assert response.status_code == 200
    with session_scope() as session:
        row = session.get(PlaudFile, "manual")
        assert row.status == FileStatus.processing
        assert row.pipeline_retry_count == 0
        assert row.pipeline_next_retry_at is None
        assert row.pipeline_last_failure_at is None


def test_reprocess_claims_synchronously_before_thread_handoff(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient

    from localplaud.api.app import app
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    audio = tmp_path / "sync-claim.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="sync-claim",
                status=FileStatus.error,
                audio_path=str(audio),
            )
        )
    handed_off = []

    class DeferredThread:
        def __init__(self, *, target, args=(), kwargs=None, daemon=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            with session_scope() as session:
                row = session.get(PlaudFile, "sync-claim")
                assert row.processing_token == self.kwargs["claim_token"]
                assert row.status == FileStatus.processing
            handed_off.append((self.target, self.args, self.kwargs))

    monkeypatch.setattr("threading.Thread", DeferredThread)
    response = TestClient(app).post("/file/sync-claim/reprocess")
    assert response.status_code == 200
    assert handed_off[0][1] == ("sync-claim",)
    assert handed_off[0][2]["claim_token"]


def test_processing_claim_is_exclusive_and_releasable(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import (
        PipelineAlreadyRunning,
        _claim_processing,
        _release_processing,
        processing_claim_active,
    )

    audio = tmp_path / "claimed.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as session:
        session.add(PlaudFile(id="claimed", status=FileStatus.downloaded, audio_path=str(audio)))

    token = _claim_processing("claimed")
    with session_scope() as session:
        row = session.get(PlaudFile, "claimed")
        assert row.status == FileStatus.processing
        assert processing_claim_active(row)
    with pytest.raises(PipelineAlreadyRunning):
        _claim_processing("claimed")

    _release_processing("claimed", token)
    replacement = _claim_processing("claimed")
    assert replacement != token
    _release_processing("claimed", replacement)


def test_expired_claim_cannot_release_with_status_but_can_cleanup_token(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _claim_processing, release_processing_claim

    with session_scope() as session:
        session.add(PlaudFile(id="expired-release", status=FileStatus.downloaded))
    token = _claim_processing("expired-release", require_audio=False)
    with session_scope() as session:
        row = session.get(PlaudFile, "expired-release")
        row.processing_lease_until = datetime.now(UTC) - timedelta(seconds=1)

    release_processing_claim(
        "expired-release",
        token,
        status=FileStatus.done,
        error="stale owner wrote status",
    )
    with session_scope() as session:
        row = session.get(PlaudFile, "expired-release")
        assert row.processing_token == token
        assert row.status == FileStatus.processing
        assert row.error is None

    release_processing_claim("expired-release", token)
    with session_scope() as session:
        row = session.get(PlaudFile, "expired-release")
        assert row.processing_token is None
        assert row.status == FileStatus.processing


def test_manual_resume_rejects_active_processing_claim(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient

    from localplaud.api.app import app
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _claim_processing, _release_processing

    audio = tmp_path / "active.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="active",
                status=FileStatus.downloaded,
                audio_path=str(audio),
                pipeline_retry_count=2,
            )
        )
    token = _claim_processing("active")
    try:
        response = TestClient(app).post("/file/active/reprocess")
        assert response.status_code == 409
        with session_scope() as session:
            row = session.get(PlaudFile, "active")
            assert row.status == FileStatus.processing
            assert row.pipeline_retry_count == 2
    finally:
        _release_processing("active", token)


def test_setup_failure_releases_claim_and_schedules_retry(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    audio = tmp_path / "setup-failure.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="setup-failure",
                status=FileStatus.downloaded,
                audio_path=str(audio),
            )
        )
    monkeypatch.setattr(
        pipeline,
        "_process_file_claimed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("profile invalid")),
    )

    with pytest.raises(RuntimeError, match="profile invalid"):
        pipeline.process_file("setup-failure", settings=settings)
    with session_scope() as session:
        row = session.get(PlaudFile, "setup-failure")
        assert row.status == FileStatus.error
        assert row.error == "profile invalid"
        assert row.pipeline_retry_count == 1
        assert row.pipeline_next_retry_at is not None
        assert row.processing_token is None
        assert row.processing_lease_until is None


def test_overlong_recording_is_skipped_until_transcribed(monkeypatch, tmp_path):
    """Length-cap recordings never enter the automatic queue before ASR, but
    one that already has a transcript may finish its remaining stages."""
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope

    audio = tmp_path / "cap.wav"
    audio.write_bytes(b"RIFF")
    now = datetime.now(UTC)
    cap_ms = 300 * 60 * 1000
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id="cap-fresh",
                    status=FileStatus.downloaded,
                    audio_path=str(audio),
                    duration_ms=cap_ms,
                    start_time_ms=int(now.timestamp() * 1000),
                ),
                PlaudFile(
                    id="cap-transcribed",
                    status=FileStatus.partial,
                    audio_path=str(audio),
                    duration_ms=cap_ms,
                    pipeline_retry_count=1,
                    pipeline_next_retry_at=now - timedelta(minutes=1),
                ),
            ]
        )
        session.add(
            StageRun(
                file_id="cap-transcribed",
                stage=StageName.transcribe,
                status=StageStatus.completed,
                attempts=1,
                detail={},
            )
        )
    seen: list[str] = []
    monkeypatch.setattr(
        pipeline, "process_file", lambda file_id, *_args, **_kwargs: seen.append(file_id)
    )
    assert pipeline.process_pending(settings) == 1
    assert seen == ["cap-transcribed"]


@pytest.mark.parametrize(
    "source,speech_status,expected",
    [
        ("local", "completed", "derived"),
        ("plaud", "completed", None),
        ("local", "degraded", None),
        ("local", "failed", None),
    ],
)
def test_automatic_retry_infers_completed_local_speech_without_audio(
    monkeypatch, tmp_path, source, speech_status, expected
):
    settings = _reset(monkeypatch, tmp_path)
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
    )
    from localplaud.worker.pipeline import _pending_scope

    row = PlaudFile(id="resume", status=FileStatus.partial, pipeline_retry_count=1)
    row.transcripts = [Transcript(source=source, text="local raw audio result", segments=[])]
    row.stage_runs = [
        StageRun(stage=StageName.transcribe, status=StageStatus.completed),
        StageRun(stage=StageName.diarize, status=StageStatus(speech_status)),
        StageRun(stage=StageName.summarize, status=StageStatus.failed),
    ]
    assert _pending_scope(row, settings, datetime.now(UTC)) == expected
    if expected:
        row.pipeline_next_retry_at = datetime.now(UTC) + timedelta(hours=1)
        assert _pending_scope(row, settings, datetime.now(UTC)) is None


def test_new_arrival_prevents_remaining_retry_batch(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    audio = tmp_path / "late.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as s:
        s.add_all(
            [
                PlaudFile(
                    id=f"old-{i}",
                    status=FileStatus.partial,
                    audio_path=str(audio),
                    pipeline_retry_count=0,
                )
                for i in range(2)
            ]
        )
    seen = []

    def process(fid, *_args, **_kwargs):
        seen.append(fid)
        with session_scope() as s:
            s.get(PlaudFile, fid).status = FileStatus.done
            if len(seen) == 1:
                s.add(PlaudFile(id="new", status=FileStatus.downloaded, audio_path=str(audio)))

    monkeypatch.setattr(pipeline, "process_file", process)
    assert pipeline.process_pending(settings, limit=2) == 1
    assert len(seen) == 1
    assert pipeline.process_pending(settings, limit=1) == 1
    assert seen[-1] == "new"


def test_fresh_audio_precedes_both_derived_and_speech_repairs(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope

    audio = tmp_path / "mixed.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as s:
        s.add_all(
            [
                PlaudFile(id="fresh", status=FileStatus.downloaded, audio_path=str(audio)),
                PlaudFile(id="old-speech", status=FileStatus.partial, audio_path=str(audio)),
                PlaudFile(
                    id="old-notes",
                    status=FileStatus.partial,
                    stage_runs=[
                        StageRun(
                            stage=StageName.summarize,
                            status=StageStatus.pending,
                            detail={"derived_only": True},
                        )
                    ],
                ),
            ]
        )
    seen = []

    def run(fid, *args, **kwargs):
        seen.append(fid)
        with session_scope() as s:
            s.get(PlaudFile, fid).status = FileStatus.done

    monkeypatch.setattr(pipeline, "process_file", run)
    monkeypatch.setattr(pipeline, "process_derived_artifacts", run)
    pipeline.process_pending(settings, limit=2)
    assert seen[0] == "fresh"


def test_newest_recording_keeps_priority_after_failure(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.worker import pipeline

    audio = tmp_path / "queue.wav"
    audio.write_bytes(b"RIFF")
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id="latest",
                    status=FileStatus.partial,
                    audio_path=str(audio),
                    start_time_ms=int(now.timestamp() * 1000),
                    pipeline_retry_count=1,
                    pipeline_next_retry_at=now - timedelta(hours=1),
                ),
                PlaudFile(
                    id="old",
                    status=FileStatus.partial,
                    audio_path=str(audio),
                    start_time_ms=int((now - timedelta(days=30)).timestamp() * 1000),
                    pipeline_retry_count=1,
                    pipeline_next_retry_at=now - timedelta(seconds=1),
                ),
            ]
        )
    seen = []
    monkeypatch.setattr(pipeline, "process_file", lambda fid, *a, **kw: seen.append(fid))
    assert pipeline.process_pending(settings, limit=1) == 1
    assert seen == ["latest"]


@pytest.mark.parametrize(
    "source,completed,expected",
    [("local", False, 0), ("cloud", False, 1), (None, True, 0), (None, False, 1)],
)
def test_untranscribed_only_excludes_local_work_but_not_cloud_imports(
    monkeypatch, tmp_path, source, completed, expected
):
    settings = _reset(monkeypatch, tmp_path)
    settings.pipeline.auto_process_untranscribed_only = True
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
    )
    from localplaud.db.session import session_scope
    from localplaud.worker import pipeline

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as s:
        r = PlaudFile(id="recording", status=FileStatus.downloaded, audio_path=str(audio))
        if source:
            r.transcripts = [Transcript(source=source, provider="test", text="transcript")]
        if completed:
            r.stage_runs = [StageRun(stage=StageName.transcribe, status=StageStatus.completed)]
        s.add(r)
    seen = []
    monkeypatch.setattr(pipeline, "process_file", lambda fid, *a, **k: seen.append(fid))
    assert pipeline.process_pending(settings, limit=1) == expected
    assert len(seen) == expected


def test_untranscribed_only_does_not_drain_old_index_backlogs(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    settings.pipeline.auto_process_untranscribed_only = True
    from localplaud import cli

    monkeypatch.setattr("localplaud.worker.pipeline.process_pending", lambda *a, **k: 1)

    def forbidden(*a, **k):
        pytest.fail("historical backfill must stay paused")

    calls = []
    monkeypatch.setattr(
        "localplaud.worker.reindex.process_pending_reindexes", lambda *a, **k: calls.append(k)
    )
    monkeypatch.setattr("localplaud.worker.knowledge_index.process_pending_documents", forbidden)
    assert cli.process_automatic_pending(settings) == 1
    assert calls == [{"limit": settings.pipeline.files_per_cycle, "recent_only": True}]


@pytest.mark.parametrize("age_hours,window,expected", [(2, 72, 1), (200, 72, 0), (2, 0, 0)])
def test_untranscribed_only_still_retries_recent_recordings(
    monkeypatch, tmp_path, age_hours, window, expected
):
    from datetime import UTC, datetime, timedelta

    settings = _reset(monkeypatch, tmp_path)
    settings.pipeline.auto_process_untranscribed_only = True
    settings.pipeline.untranscribed_only_retry_hours = window
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker import pipeline

    now = datetime.now(UTC)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as s:
        row = PlaudFile(
            id="recording",
            status=FileStatus.partial,
            audio_path=str(audio),
            pipeline_retry_count=1,
            pipeline_next_retry_at=now - timedelta(minutes=1),
        )
        row.stage_runs = [
            StageRun(
                stage=StageName.transcribe,
                status=StageStatus.completed,
                completed_at=now - timedelta(hours=age_hours),
            ),
            StageRun(stage=StageName.correct, status=StageStatus.failed),
        ]
        s.add(row)
    seen = []
    monkeypatch.setattr(pipeline, "process_file", lambda fid, *a, **k: seen.append(fid))
    assert pipeline.process_pending(settings, limit=1) == expected
    assert len(seen) == expected


def test_untranscribed_only_resumes_recent_recording_interrupted_by_restart(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    settings = _reset(monkeypatch, tmp_path)
    settings.pipeline.auto_process_untranscribed_only = True
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker import pipeline

    now = datetime.now(UTC)
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as s:
        for fid, age in (("recent", 2), ("old", 500)):
            row = PlaudFile(id=fid, status=FileStatus.downloaded, audio_path=str(audio))
            row.stage_runs = [
                StageRun(
                    stage=StageName.transcribe,
                    status=StageStatus.completed,
                    completed_at=now - timedelta(hours=age),
                ),
                StageRun(
                    stage=StageName.correct,
                    status=StageStatus.failed,
                    error="Interrupted by application restart; queued for retry.",
                ),
            ]
            s.add(row)
    seen = []
    monkeypatch.setattr(pipeline, "process_file", lambda fid, *a, **k: seen.append(fid))
    assert pipeline.process_pending(settings) == 1
    assert seen == ["recent"]


def test_retry_gate_keeps_legacy_stopped_rows_stopped(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    from localplaud.worker.pipeline import _retry_not_due

    now = datetime.now(UTC)
    maximum = settings.pipeline.retry_max_attempts
    assert _retry_not_due(maximum, None, settings, now) is True
    assert _retry_not_due(maximum + 2, None, settings, now) is True
    assert _retry_not_due(maximum, now - timedelta(seconds=1), settings, now) is False
    assert _retry_not_due(maximum, now + timedelta(seconds=60), settings, now) is True
    assert _retry_not_due(0, None, settings, now) is False


def test_fallback_title_prefers_note_text_then_recording_date():
    from localplaud.worker.pipeline import _fallback_title_text

    result = {"content_md": "## 覆核備註\n- x\n\n討論黑客松成果與下一步規劃。其他細節。"}
    assert _fallback_title_text(result, None) == "討論黑客松成果與下一步規劃"
    started = datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
    assert _fallback_title_text({"content_md": "短"}, started).endswith("錄音")
    assert _fallback_title_text({}, None) == "未命名錄音"


def test_speech_stage_is_retried_after_a_resource_failure_and_nothing_else(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.asr.base import AsrError, AsrResourceError
    from localplaud.remote.client import RemoteWorkerError

    settings.pipeline.speech_resource_retries = 2
    settings.pipeline.speech_resource_retry_seconds = 7
    sleeps = []
    monkeypatch.setattr(pipeline.time, "sleep", sleeps.append)
    monkeypatch.setattr(pipeline, "_renew_processing_claim", lambda file_id: None)

    def failing_then_ok(errors):
        queue = list(errors)

        def operation():
            if queue:
                raise queue.pop(0)
            return "diarized"

        return operation

    killed = RemoteWorkerError("exit -9", retryable=True, code="worker_resource_exhausted")
    assert pipeline._retry_resource_failures("f", settings, failing_then_ok([killed])) == "diarized"
    assert sleeps == [7]

    sleeps.clear()
    local = AsrResourceError("killed")
    assert (
        pipeline._retry_resource_failures("f", settings, failing_then_ok([local, killed]))
        == "diarized"
    )
    assert sleeps == [7, 7]

    # A third consecutive failure is not retried again: the stage degrades as before.
    with pytest.raises(AsrResourceError):
        pipeline._retry_resource_failures("f", settings, failing_then_ok([local, local, local]))

    # Deterministic and merely retryable (timeout) failures are never repeated here.
    for error in (
        AsrError("bad audio"),
        RemoteWorkerError("timed out", retryable=True),
        ValueError("x"),
    ):
        sleeps.clear()
        with pytest.raises(type(error)):
            pipeline._retry_resource_failures("f", settings, failing_then_ok([error]))
        assert sleeps == []

    settings.pipeline.speech_resource_retries = 0
    with pytest.raises(RemoteWorkerError):
        pipeline._retry_resource_failures("f", settings, failing_then_ok([killed]))
