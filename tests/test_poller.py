"""Tests for poller recovery + change detection (review fixes #1, #5)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta


def _reset_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    return get_settings(reload=True)


def test_reset_inflight_recovers_crashed_rows(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageAttempt,
        StageName,
        StageRun,
        StageStatus,
    )
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import reset_inflight

    init_db()
    with session_scope() as s:
        s.add(PlaudFile(id="dl", status=FileStatus.downloading))
        s.add(
            PlaudFile(
                id="pr",
                status=FileStatus.processing,
                audio_path="/x",
                processing_token="stale-token",
                processing_lease_until=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        s.add(PlaudFile(id="ok", status=FileStatus.done))
        s.add(
            PlaudFile(
                id="partial-claim",
                status=FileStatus.partial,
                audio_path="/x",
                processing_token="orphan-partial",
                processing_lease_until=datetime.now(UTC) + timedelta(hours=12),
            )
        )
        s.add(
            PlaudFile(
                id="error-claim",
                status=FileStatus.error,
                audio_path="/x",
                processing_token="orphan-error",
                processing_lease_until=datetime.now(UTC) + timedelta(hours=12),
            )
        )
        s.add(
            StageRun(
                file_id="pr",
                stage=StageName.transcribe,
                status=StageStatus.running,
                attempts=1,
            )
        )
        s.add(
            StageAttempt(
                file_id="pr",
                stage=StageName.transcribe,
                attempt=1,
                status=StageStatus.running,
            )
        )

    n = reset_inflight(force=True)
    assert n == 4
    with session_scope() as s:
        assert s.get(PlaudFile, "dl").status == FileStatus.discovered
        assert s.get(PlaudFile, "pr").status == FileStatus.downloaded
        assert s.get(PlaudFile, "pr").processing_token is None
        assert s.get(PlaudFile, "pr").processing_lease_until is None
        assert s.get(PlaudFile, "ok").status == FileStatus.done  # untouched
        for file_id in ("partial-claim", "error-claim"):
            row = s.get(PlaudFile, file_id)
            assert row.processing_token is None
            assert row.processing_lease_until is None
        run = s.query(StageRun).one()
        attempt = s.query(StageAttempt).one()
        assert run.status == StageStatus.failed
        assert attempt.status == StageStatus.failed
        assert run.completed_at is not None
        assert attempt.completed_at is not None
        assert "application restart" in run.error
        assert "application restart" in attempt.error


def test_periodic_recovery_preserves_live_processing_lease(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageAttempt,
        StageName,
        StageRun,
        StageStatus,
    )
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import reset_inflight

    init_db()
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="live-worker",
                status=FileStatus.processing,
                audio_path="/x",
                processing_token="live-token",
                processing_lease_until=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.add(
            StageRun(
                file_id="live-worker",
                stage=StageName.correct,
                status=StageStatus.running,
                attempts=1,
            )
        )
        session.add(
            StageAttempt(
                file_id="live-worker",
                stage=StageName.correct,
                attempt=1,
                status=StageStatus.running,
            )
        )

    assert reset_inflight() == 0
    with session_scope() as session:
        row = session.get(PlaudFile, "live-worker")
        assert row.status == FileStatus.processing
        assert row.processing_token == "live-token"
        assert session.query(StageRun).one().status == StageStatus.running
        assert session.query(StageAttempt).one().status == StageStatus.running


def test_reset_download_errors_retries_only_audioless_rows(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import reset_download_errors

    init_db()
    with session_scope() as s:
        # Download-stage failure (429/network): no audio on disk -> retry.
        s.add(PlaudFile(id="dl-err", status=FileStatus.error, error="429"))
        # Pipeline failure: audio exists -> NOT a download problem, keep it.
        s.add(
            PlaudFile(
                id="pipe-err", status=FileStatus.error, audio_path="/a.mp3", error="ollama down"
            )
        )

    assert reset_download_errors() == 1
    with session_scope() as s:
        assert s.get(PlaudFile, "dl-err").status == FileStatus.discovered
        assert s.get(PlaudFile, "dl-err").error is None
        assert s.get(PlaudFile, "pipe-err").status == FileStatus.error


def test_catalog_sync_claim_serializes_concurrent_first_listing(monkeypatch, tmp_path):
    settings = _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, KeyValue, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.plaud.models import PlaudFileDTO
    from localplaud.poller.poll import sync_file_list

    init_db()
    entered = threading.Event()
    release = threading.Event()
    result: list[tuple[int, int]] = []

    class BlockingClient:
        def iter_files(self, include_trash=False):
            entered.set()
            assert release.wait(timeout=5)
            yield PlaudFileDTO(id="baseline", filename="Baseline")

    worker = threading.Thread(
        target=lambda: result.append(sync_file_list(BlockingClient(), settings))
    )
    worker.start()
    assert entered.wait(timeout=5)
    assert sync_file_list(BlockingClient(), settings) == (0, 0)
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive() and result == [(1, 0)]

    with session_scope() as session:
        assert session.get(PlaudFile, "baseline").status == FileStatus.metadata_only
        assert session.get(KeyValue, "plaud_catalog_baseline_v1") is not None
        assert session.get(KeyValue, "plaud_catalog_sync_lock_v1") is None


def test_failed_paginated_listing_rolls_back_baseline_and_releases_claim(
    monkeypatch, tmp_path
):
    settings = _reset_db(monkeypatch, tmp_path)
    import pytest

    from localplaud.db.models import KeyValue, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.plaud.models import PlaudFileDTO
    from localplaud.poller.poll import sync_file_list

    init_db()

    class FailingClient:
        def iter_files(self, include_trash=False):
            yield PlaudFileDTO(id="partial-page", filename="Partial page")
            raise RuntimeError("next page unavailable")

    with pytest.raises(RuntimeError, match="next page unavailable"):
        sync_file_list(FailingClient(), settings)

    with session_scope() as session:
        assert session.get(PlaudFile, "partial-page") is None
        assert session.get(KeyValue, "plaud_catalog_baseline_v1") is None
        assert session.get(KeyValue, "plaud_catalog_sync_lock_v1") is None


def test_stale_catalog_sync_claim_is_recovered(monkeypatch, tmp_path):
    settings = _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import KeyValue
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import sync_file_list

    init_db()
    stale = datetime.now(UTC) - timedelta(hours=1)
    with session_scope() as session:
        session.add(
            KeyValue(
                key="plaud_catalog_sync_lock_v1",
                value={"token": "crashed", "claimed_at": stale.isoformat()},
                updated_at=stale,
            )
        )

    class EmptyClient:
        def iter_files(self, include_trash=False):
            return iter(())

    assert sync_file_list(EmptyClient(), settings) == (0, 0)
    with session_scope() as session:
        assert session.get(KeyValue, "plaud_catalog_baseline_v1") is not None
        assert session.get(KeyValue, "plaud_catalog_sync_lock_v1") is None


def test_download_claim_prevents_duplicate_cloud_fetch(monkeypatch, tmp_path):
    settings = _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import _download_one

    init_db()
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="new-audio",
                filename="New audio",
                status=FileStatus.discovered,
                raw={"id": "new-audio", "filename": "New audio"},
            )
        )

    entered = threading.Event()
    release = threading.Event()

    class BlockingClient:
        calls = 0

        def download_audio(self, dto, destination):
            self.calls += 1
            entered.set()
            assert release.wait(timeout=5)
            path = destination / "audio.opus"
            path.write_bytes(b"audio")
            return path

    client = BlockingClient()
    result: list[bool] = []
    worker = threading.Thread(
        target=lambda: result.append(
            _download_one(client, "new-audio", {"id": "new-audio"}, settings)
        )
    )
    worker.start()
    assert entered.wait(timeout=5)
    assert _download_one(client, "new-audio", {"id": "new-audio"}, settings) is False
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive() and result == [True]
    assert client.calls == 1
    with session_scope() as session:
        row = session.get(PlaudFile, "new-audio")
        assert row.status == FileStatus.downloaded
        assert row.audio_path
