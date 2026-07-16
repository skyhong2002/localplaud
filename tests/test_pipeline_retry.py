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
    monkeypatch.setattr(
        pipeline, "process_file", lambda file_id, *_args, **_kwargs: seen.append(file_id)
    )
    assert pipeline.process_pending(settings, limit=3) == 3
    assert seen == ["fresh", "due", "legacy"]


def test_due_retry_is_not_starved_by_older_download_backlog(monkeypatch, tmp_path):
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
    assert pipeline.process_pending(settings, limit=1) == 1
    assert seen == ["due-retry"]


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
    }
    assert {column["name"] for column in inspect(engine).get_columns("plaud_files")} >= {
        "pipeline_retry_count",
        "pipeline_next_retry_at",
        "pipeline_last_failure_at",
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
        session.add(
            PlaudFile(id="claimed", status=FileStatus.downloaded, audio_path=str(audio))
        )

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


def test_expired_claim_cannot_release_with_status_but_can_cleanup_token(
    monkeypatch, tmp_path
):
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
