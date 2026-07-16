"""Tests for poller recovery + change detection (review fixes #1, #5)."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


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
    stale_download = datetime.now(UTC) - timedelta(hours=2)
    with session_scope() as s:
        s.add(
            PlaudFile(
                id="dl",
                status=FileStatus.downloading,
                created_at=stale_download,
                updated_at=stale_download,
            )
        )
        s.add(
            PlaudFile(
                id="pr",
                status=FileStatus.processing,
                audio_path="/x",
                processing_token="stale-token",
                processing_lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        s.add(PlaudFile(id="ok", status=FileStatus.done))
        s.add(
            PlaudFile(
                id="live-processing",
                status=FileStatus.processing,
                audio_path="/x",
                processing_token="live-processing-token",
                processing_lease_until=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        s.add(
            PlaudFile(
                id="partial-claim",
                status=FileStatus.partial,
                audio_path="/x",
                processing_token="orphan-partial",
                processing_lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        s.add(
            PlaudFile(
                id="error-claim",
                status=FileStatus.error,
                audio_path="/x",
                processing_token="orphan-error",
                processing_lease_until=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        s.add(
            PlaudFile(
                id="live-reindex",
                status=FileStatus.done,
                audio_path="/x",
                processing_token="live-reindex-token",
                processing_lease_until=datetime.now(UTC) + timedelta(hours=1),
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
        live_processing = s.get(PlaudFile, "live-processing")
        assert live_processing.status == FileStatus.processing
        assert live_processing.processing_token == "live-processing-token"
        for file_id in ("partial-claim", "error-claim"):
            row = s.get(PlaudFile, file_id)
            assert row.processing_token is None
            assert row.processing_lease_until is None
        live_reindex = s.get(PlaudFile, "live-reindex")
        assert live_reindex.processing_token == "live-reindex-token"
        assert live_reindex.processing_lease_until.replace(tzinfo=UTC) > datetime.now(UTC)
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


def test_startup_recovers_only_previous_daemon_and_expired_claims(monkeypatch, tmp_path):
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
    live_lease = datetime.now(UTC) + timedelta(hours=1)
    expired_lease = datetime.now(UTC) - timedelta(seconds=1)
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id="old-daemon-processing",
                    status=FileStatus.processing,
                    audio_path="/x",
                    processing_token="daemon:old-owner:processing",
                    processing_lease_until=live_lease,
                ),
                PlaudFile(
                    id="old-daemon-reindex",
                    status=FileStatus.done,
                    audio_path="/x",
                    processing_token="daemon:old-owner:reindex",
                    processing_lease_until=live_lease,
                ),
                PlaudFile(
                    id="unrelated-live",
                    status=FileStatus.processing,
                    audio_path="/x",
                    processing_token="cli-worker-token",
                    processing_lease_until=live_lease,
                ),
                PlaudFile(
                    id="unrelated-expired",
                    status=FileStatus.processing,
                    audio_path="/x",
                    processing_token="expired-cli-token",
                    processing_lease_until=expired_lease,
                ),
            ]
        )
        for file_id, stage in (
            ("old-daemon-processing", StageName.transcribe),
            ("old-daemon-reindex", StageName.index),
            ("unrelated-live", StageName.correct),
        ):
            session.add(
                StageRun(
                    file_id=file_id,
                    stage=stage,
                    status=StageStatus.running,
                    attempts=1,
                    detail={"reindex_only": True} if stage == StageName.index else {},
                )
            )
            session.add(
                StageAttempt(
                    file_id=file_id,
                    stage=stage,
                    attempt=1,
                    status=StageStatus.running,
                )
            )

    assert reset_inflight(force=True, previous_owner="old-owner") == 3

    with session_scope() as session:
        old_processing = session.get(PlaudFile, "old-daemon-processing")
        assert old_processing.status == FileStatus.downloaded
        assert old_processing.processing_token is None
        old_reindex = session.get(PlaudFile, "old-daemon-reindex")
        assert old_reindex.status == FileStatus.done
        assert old_reindex.processing_token is None
        unrelated_live = session.get(PlaudFile, "unrelated-live")
        assert unrelated_live.status == FileStatus.processing
        assert unrelated_live.processing_token == "cli-worker-token"
        expired = session.get(PlaudFile, "unrelated-expired")
        assert expired.status == FileStatus.downloaded
        assert expired.processing_token is None
        runs = {row.file_id: row for row in session.query(StageRun)}
        attempts = {row.file_id: row for row in session.query(StageAttempt)}
        assert runs["old-daemon-processing"].status == StageStatus.failed
        assert runs["old-daemon-reindex"].status == StageStatus.pending
        assert runs["unrelated-live"].status == StageStatus.running
        assert attempts["old-daemon-processing"].status == StageStatus.failed
        assert attempts["old-daemon-reindex"].status == StageStatus.failed
        assert attempts["unrelated-live"].status == StageStatus.running

    calls: list[str] = []
    monkeypatch.setattr(
        "localplaud.worker.reindex.reindex_file",
        lambda file_id, _settings: calls.append(file_id) or True,
    )
    from localplaud.worker.reindex import process_pending_reindexes

    assert process_pending_reindexes(limit=10) == 1
    assert calls == ["old-daemon-reindex"]


def test_startup_preserves_recent_unowned_download(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import reset_inflight

    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="live-download", status=FileStatus.downloading))

    assert reset_inflight(force=True, previous_owner="old-owner") == 0
    with session_scope() as session:
        assert session.get(PlaudFile, "live-download").status == FileStatus.downloading


def test_daemon_owner_registration_rejects_live_owner_and_recovers_dead_owner(
    monkeypatch, tmp_path
):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import KeyValue
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller import poll as poll_module

    init_db()
    monkeypatch.setattr(poll_module, "_pid_is_running", lambda _pid: True)
    first_owner, previous_owner = poll_module.register_daemon_owner()
    assert previous_owner is None
    assert len(first_owner) == 16
    with pytest.raises(RuntimeError, match="already running"):
        poll_module.register_daemon_owner()

    monkeypatch.setattr(poll_module, "_pid_is_running", lambda _pid: False)
    second_owner, previous_owner = poll_module.register_daemon_owner()
    assert previous_owner == first_owner
    assert second_owner != first_owner
    assert poll_module.refresh_daemon_owner("wrong-owner") is False
    assert poll_module.refresh_daemon_owner(second_owner) is True
    with session_scope() as session:
        value = session.get(KeyValue, "localplaud_daemon_owner_v1").value
        assert value["owner"] == second_owner
        assert value["heartbeat_at"]
    assert poll_module.release_daemon_owner(second_owner)


def test_daemon_owner_detects_pid_reuse_and_graceful_release(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    import socket

    from localplaud.db.models import KeyValue
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller import poll as poll_module

    init_db()
    monkeypatch.setattr(poll_module, "_pid_is_running", lambda _pid: True)
    monkeypatch.setattr(poll_module, "_process_start_fingerprint", lambda _pid: "new-birth")
    with session_scope() as session:
        session.add(
            KeyValue(
                key="localplaud_daemon_owner_v1",
                value={
                    "owner": "reused-pid-owner",
                    "hostname": socket.gethostname(),
                    "pid": 4242,
                    "process_start_fingerprint": "old-birth",
                    "heartbeat_at": datetime.now(UTC).isoformat(),
                },
            )
        )

    owner, previous = poll_module.register_daemon_owner()
    assert previous == "reused-pid-owner"
    assert poll_module.current_daemon_owner() == owner
    assert poll_module.release_daemon_owner(owner) is True
    assert poll_module.current_daemon_owner() is None
    assert poll_module.refresh_daemon_owner(owner) is False
    replacement, previous = poll_module.register_daemon_owner()
    assert previous == owner
    assert replacement != owner
    assert poll_module.release_daemon_owner(replacement)


def test_periodic_processing_reset_is_one_atomic_postgresql_update():
    from sqlalchemy.dialects import postgresql

    from localplaud.poller.poll import _processing_reset_statement

    compiled = str(
        _processing_reset_statement(now=datetime.now(UTC), force=False).compile(
            dialect=postgresql.dialect()
        )
    )
    assert compiled.lstrip().startswith("UPDATE plaud_files")
    assert "processing_lease_until IS NULL" in compiled
    assert "processing_lease_until <=" in compiled
    assert "RETURNING plaud_files.id" in compiled
    forced = str(
        _processing_reset_statement(
            now=datetime.now(UTC), force=True, previous_owner="old-owner"
        ).compile(dialect=postgresql.dialect())
    )
    assert "processing_lease_until IS NULL" in forced
    assert "processing_lease_until <=" in forced
    assert "processing_token LIKE" in forced


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


def test_failed_paginated_listing_rolls_back_baseline_and_releases_claim(monkeypatch, tmp_path):
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


def test_first_poll_neutralizes_old_download_queue_without_fetching(monkeypatch, tmp_path):
    settings = _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.plaud.models import PlaudFileDTO
    from localplaud.poller.poll import poll_once

    init_db()
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="old-download-error",
                filename="Old download error",
                origin="plaud",
                status=FileStatus.error,
                error="old failure",
            )
        )

    class BaselineClient:
        downloads = 0

        def iter_files(self, include_trash=False):
            yield PlaudFileDTO(id="old-download-error", filename="Old download error")

        def download_audio(self, *_args):
            self.downloads += 1
            raise AssertionError("the baseline cycle must not download historical audio")

    client = BaselineClient()

    @contextmanager
    def fake_factory(_config):
        yield client

    monkeypatch.setattr("localplaud.poller.poll.make_plaud_client", fake_factory)
    result = poll_once(settings)

    assert result["new"] == 0 and result["downloaded"] == 0
    assert client.downloads == 0
    with session_scope() as session:
        row = session.get(PlaudFile, "old-download-error")
        assert row.status == FileStatus.metadata_only
        assert row.error is None


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


def test_download_batch_propagates_daemon_owner_to_threads(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    settings = _reset_db(monkeypatch, tmp_path)
    settings.poller.max_concurrent_downloads = 2
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import download_pending
    from localplaud.worker.claims import current_processing_owner, processing_owner

    init_db()
    with session_scope() as session:
        session.add_all(
            [PlaudFile(id=f"owned-{index}", status=FileStatus.discovered) for index in range(2)]
        )
    observed: list[str | None] = []

    class Client:
        def download_audio(self, dto, destination):
            observed.append(current_processing_owner())
            path = destination / f"{dto.id}.opus"
            path.write_bytes(b"audio")
            return path

    with processing_owner("daemon-owner"):
        assert download_pending(Client(), settings) == 2
    assert sorted(observed) == ["daemon-owner", "daemon-owner"]


def test_preclaimed_web_download_uses_active_daemon_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    settings = _reset_db(monkeypatch, tmp_path)
    import localplaud.poller.poll as poll_module
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope

    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="web-import", status=FileStatus.downloading))
    observed: list[str] = []

    class Client:
        def download_audio(self, _dto, destination):
            with session_scope() as session:
                observed.append(session.get(PlaudFile, "web-import").download_token)
            path = destination / "audio.opus"
            path.write_bytes(b"audio")
            return path

    monkeypatch.setattr(poll_module, "_ACTIVE_DAEMON_OWNER", "daemon-owner")
    assert poll_module._download_one(
        Client(),
        "web-import",
        {"id": "web-import"},
        settings,
        claim_acquired=True,
    )
    assert len(observed) == 1 and observed[0].startswith("daemon:daemon-owner:")


def test_recovered_download_cannot_publish_over_new_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    settings = _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import _download_one, reset_inflight
    from localplaud.worker.claims import processing_owner

    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="takeover", status=FileStatus.discovered))
    entered = threading.Event()
    release = threading.Event()
    old_result: list[bool] = []

    class OldClient:
        def download_audio(self, _dto, destination):
            entered.set()
            assert release.wait(timeout=5)
            path = destination / "audio.opus"
            path.write_bytes(b"old")
            return path

    def run_old():
        with processing_owner("old-owner"):
            old_result.append(_download_one(OldClient(), "takeover", {"id": "takeover"}, settings))

    worker = threading.Thread(target=run_old)
    worker.start()
    assert entered.wait(timeout=5)
    with session_scope() as session:
        token = session.get(PlaudFile, "takeover").download_token
        assert token.startswith("daemon:old-owner:")
    assert reset_inflight(force=True, previous_owner="old-owner") == 1

    class NewClient:
        def download_audio(self, _dto, destination):
            path = destination / "audio.opus"
            path.write_bytes(b"new")
            return path

    with processing_owner("new-owner"):
        assert _download_one(NewClient(), "takeover", {"id": "takeover"}, settings)
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive() and old_result == [False]
    with session_scope() as session:
        row = session.get(PlaudFile, "takeover")
        assert row.status == FileStatus.downloaded
        assert row.download_token is None and row.download_lease_until is None
        published = row.audio_path
    assert published and Path(published).read_bytes() == b"new"


def test_displaced_download_error_cannot_overwrite_new_success(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    settings = _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import _download_one, reset_inflight
    from localplaud.worker.claims import processing_owner

    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="late-error", status=FileStatus.discovered))
    entered = threading.Event()
    release = threading.Event()

    class FailingClient:
        def download_audio(self, _dto, _destination):
            entered.set()
            assert release.wait(timeout=5)
            raise RuntimeError("late old failure")

    def run_old_failure():
        with processing_owner("old-owner"):
            _download_one(FailingClient(), "late-error", {"id": "late-error"}, settings)

    worker = threading.Thread(target=run_old_failure)
    worker.start()
    assert entered.wait(timeout=5)
    assert reset_inflight(force=True, previous_owner="old-owner") == 1

    class NewClient:
        def download_audio(self, _dto, destination):
            path = destination / "audio.opus"
            path.write_bytes(b"new")
            return path

    with processing_owner("new-owner"):
        assert _download_one(NewClient(), "late-error", {"id": "late-error"}, settings)
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    with session_scope() as session:
        row = session.get(PlaudFile, "late-error")
        assert row.status == FileStatus.downloaded
        assert row.error is None
        published = row.audio_path
    assert published and Path(published).read_bytes() == b"new"


def test_expired_download_lease_cannot_publish(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    settings = _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import _download_one, reset_inflight

    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="expired-download", status=FileStatus.discovered))

    class ExpiringClient:
        def download_audio(self, _dto, destination):
            path = destination / "audio.opus"
            path.write_bytes(b"late")
            with session_scope() as session:
                session.get(PlaudFile, "expired-download").download_lease_until = (
                    datetime.now(UTC) - timedelta(seconds=1)
                )
            return path

    assert not _download_one(
        ExpiringClient(), "expired-download", {"id": "expired-download"}, settings
    )
    with session_scope() as session:
        row = session.get(PlaudFile, "expired-download")
        assert row.status == FileStatus.downloading
        assert row.audio_path is None
    assert reset_inflight() == 1
    with session_scope() as session:
        row = session.get(PlaudFile, "expired-download")
        assert row.status == FileStatus.discovered
        assert row.download_token is None
