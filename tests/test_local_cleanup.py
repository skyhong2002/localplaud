"""Explicit local-only audio and derived-artifact cleanup."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'clean.db'}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.models import (
        Chunk,
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        Summary,
        SummaryRevision,
        Transcript,
        TranscriptRevision,
        UserNote,
    )
    from localplaud.db.session import init_db, session_scope

    init_db()
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    audio = audio_dir / "audio.mp3"
    wav = audio_dir / "audio.wav"
    waveform = audio_dir / "waveform-180.json"
    audio.write_bytes(b"audio")
    wav.write_bytes(b"wav")
    waveform.write_text("{}")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="clean",
                filename="Cleanup",
                origin="plaud",
                status=FileStatus.done,
                audio_path=str(audio),
                wav_path=str(wav),
            )
        )
        session.add_all(
            [
                Transcript(file_id="clean", provider="local", source="local", text="local"),
                Transcript(file_id="clean", provider="plaud", source="cloud", text="cloud"),
                TranscriptRevision(
                    file_id="clean", revision=1, source="local", text="corrected"
                ),
                TranscriptRevision(
                    file_id="clean", revision=2, source="cloud", text="cloud corrected"
                ),
                Summary(
                    file_id="clean", template="default", source="local", content_md="local"
                ),
                Summary(
                    file_id="clean", template="plaud", source="cloud", content_md="cloud"
                ),
                SummaryRevision(
                    file_id="clean",
                    template="default",
                    revision=1,
                    source="local",
                    content_md="older local note",
                ),
                Chunk(file_id="clean", idx=0, text="chunk"),
                StageRun(file_id="clean", stage=StageName.index),
                UserNote(file_id="clean", title="Keep me", content_md="user-authored"),
            ]
        )
        session.add(PlaudFile(id="upload", filename="Upload", origin="local"))
    return TestClient(app), audio, wav, waveform


def test_delete_processing_preserves_cloud_and_user_data(monkeypatch, tmp_path):
    client, audio, wav, _waveform = _client(monkeypatch, tmp_path)
    from localplaud.db.models import KnowledgeChunk, KnowledgeDocument, Summary
    from localplaud.db.session import session_scope

    with session_scope() as session:
        summary = session.query(Summary).filter_by(file_id="clean", source="local").one()
        document = KnowledgeDocument(
            kind="generated_summary",
            file_id="clean",
            summary_id=summary.id,
            content_sha256="a" * 64,
            generation="generation",
            status="completed",
        )
        session.add(document)
        session.flush()
        session.add(
            KnowledgeChunk(
                document_id=document.id,
                idx=0,
                text="private local note text",
                dim=1,
                embedding=b"data",
            )
        )
    page = client.get("/file/clean")
    assert "Delete local processing" in page.text and "Remove local audio" in page.text
    response = client.delete("/api/files/clean/local-processing")
    assert response.status_code == 200
    assert response.json()["removed"] == {
        "revisions": 1,
        "transcripts": 1,
        "notes": 1,
        "note_versions": 1,
        "chunks": 1,
        "stages": 1,
        "attempts": 0,
    }
    assert audio.exists() and not wav.exists()
    from localplaud.db.models import FileStatus, PlaudFile

    with session_scope() as session:
        row = session.get(PlaudFile, "clean")
        assert row.status == FileStatus.downloaded
        assert [item.source for item in row.transcripts] == ["cloud"]
        assert [item.source for item in row.transcript_revisions] == ["cloud"]
        assert [item.source for item in row.summaries] == ["cloud"]
        assert row.summary_revisions == []
        assert len(row.user_notes) == 1 and row.user_notes[0].title == "Keep me"
        assert session.query(KnowledgeDocument).count() == 0
        assert session.query(KnowledgeChunk).count() == 0


def test_cleanup_preserves_editable_copy_and_rejects_active_claim(monkeypatch, tmp_path):
    client, _audio, _wav, _waveform = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, Summary, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        summary = session.query(Summary).filter_by(file_id="clean", source="local").one()
        note = session.query(UserNote).filter_by(file_id="clean").one()
        note.source_summary_id = summary.id
        row = session.get(PlaudFile, "clean")
        row.status = FileStatus.processing
        row.processing_token = "active"
        row.processing_lease_until = datetime.now(UTC) + timedelta(hours=1)
    assert client.delete("/api/files/clean/local-processing").status_code == 409

    with session_scope() as session:
        row = session.get(PlaudFile, "clean")
        row.status = FileStatus.done
        row.processing_token = None
        row.processing_lease_until = None
    assert client.delete("/api/files/clean/local-processing").status_code == 200
    with session_scope() as session:
        note = session.query(UserNote).filter_by(file_id="clean").one()
        assert note.title == "Keep me" and note.source_summary_id is None


def test_cleanup_rejects_active_note_index_lease(monkeypatch, tmp_path):
    client, _audio, _wav, _waveform = _client(monkeypatch, tmp_path)
    from localplaud.db.models import KnowledgeDocument, Summary
    from localplaud.db.session import session_scope

    with session_scope() as session:
        summary = session.query(Summary).filter_by(file_id="clean", source="local").one()
        session.add(
            KnowledgeDocument(
                kind="generated_summary",
                file_id="clean",
                summary_id=summary.id,
                content_sha256="b" * 64,
                generation="active-generation",
                status="running",
                lease_token="active-token",
                lease_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
    assert client.delete("/api/files/clean/local-processing").status_code == 409
    with session_scope() as session:
        document = session.query(KnowledgeDocument).one()
        document.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    assert client.delete("/api/files/clean/local-processing").status_code == 200


def test_cleanup_rejects_active_ask_evidence_lease(monkeypatch, tmp_path):
    client, _audio, wav, _waveform = _client(monkeypatch, tmp_path)
    from localplaud.db.models import AskThread
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            AskThread(
                id="active-library-ask",
                file_id=None,
                title="Active Ask",
                request_token="ask-token",
                request_lease_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
    response = client.delete("/api/files/clean/local-processing")
    assert response.status_code == 409
    assert "used by Ask" in response.json()["detail"]
    assert wav.exists()


def test_cleanup_retains_cumulative_stage_spend(monkeypatch, tmp_path):
    client, _audio, _wav, _waveform = _client(monkeypatch, tmp_path)
    from sqlalchemy import select

    from localplaud.db.models import (
        ProviderCostReservation,
        StageAttempt,
        StageName,
        StageStatus,
    )
    from localplaud.db.session import session_scope
    from localplaud.providers.usage import cost_budget_status

    with session_scope() as session:
        session.add(
            StageAttempt(
                file_id="clean",
                stage=StageName.index,
                attempt=1,
                status=StageStatus.completed,
                estimated_cost_usd=0.25,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )
    response = client.delete("/api/files/clean/local-processing")
    assert response.status_code == 200
    assert response.json()["removed"]["attempts"] == 1
    with session_scope() as session:
        assert session.scalar(select(StageAttempt)) is None
        retained = session.scalar(select(ProviderCostReservation))
        assert retained.status == "completed"
        assert retained.estimated_cost_usd == 0.25
        assert cost_budget_status(session, "clean", {"policy": {}})["spent_usd"] == 0.25


def test_bulk_resume_is_atomic_and_due_immediately(monkeypatch, tmp_path):
    client, audio, _wav, _waveform = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            PlaudFile(
                id="retry-two",
                filename="Retry two",
                status=FileStatus.error,
                audio_path=str(audio),
            )
        )
    missing = client.post(
        "/api/files/bulk",
        json={"file_ids": ["clean", "missing"], "action": "resume"},
    )
    assert missing.status_code == 404
    with session_scope() as session:
        assert session.get(PlaudFile, "clean").status == FileStatus.done

    response = client.post(
        "/api/files/bulk",
        json={"file_ids": ["clean", "retry-two"], "action": "resume"},
    )
    assert response.status_code == 200 and response.json()["updated"] == 2
    with session_scope() as session:
        for file_id in ("clean", "retry-two"):
            row = session.get(PlaudFile, file_id)
            assert row.pipeline_retry_count == 0
            assert row.pipeline_next_retry_at is not None
            retry_at = row.pipeline_next_retry_at
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            assert retry_at <= datetime.now(UTC)


def test_remove_plaud_audio_returns_to_metadata_only(monkeypatch, tmp_path):
    client, audio, wav, waveform = _client(monkeypatch, tmp_path)
    response = client.delete("/api/files/clean/local-audio")
    assert response.status_code == 200
    assert response.json()["status"] == "metadata_only"
    assert not audio.exists() and not wav.exists() and not waveform.exists()
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        row = session.get(PlaudFile, "clean")
        assert row.status == FileStatus.metadata_only


def test_remove_audio_rejects_live_download_and_clears_expired_claim(monkeypatch, tmp_path):
    client, audio, _wav, _waveform = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        row = session.get(PlaudFile, "clean")
        row.status = FileStatus.downloading
        row.download_token = "live-download"
        row.download_lease_until = datetime.now(UTC) + timedelta(minutes=5)
    blocked = client.delete("/api/files/clean/local-audio")
    assert blocked.status_code == 409
    assert audio.exists()

    with session_scope() as session:
        row = session.get(PlaudFile, "clean")
        row.status = FileStatus.done
    assert client.delete("/api/files/clean/local-audio").status_code == 409

    with session_scope() as session:
        row = session.get(PlaudFile, "clean")
        row.download_lease_until = datetime.now(UTC) - timedelta(seconds=1)
    removed = client.delete("/api/files/clean/local-audio")
    assert removed.status_code == 200
    with session_scope() as session:
        row = session.get(PlaudFile, "clean")
        assert row.status == FileStatus.metadata_only
        assert row.download_token is None
        assert row.download_lease_until is None
        assert row.audio_path is None and row.wav_path is None
        assert len(row.transcripts) == 2 and len(row.summaries) == 2
    assert client.delete("/api/files/upload/local-audio").status_code == 409
    assert client.delete("/api/files/missing/local-audio").status_code == 404


@pytest.mark.parametrize("operation", ["audio", "processing"])
def test_cleanup_restores_quarantined_files_when_commit_fails(
    monkeypatch, tmp_path, operation
):
    _client(monkeypatch, tmp_path)
    import localplaud.local_cleanup as cleanup
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope as real_session_scope

    @contextmanager
    def failing_session_scope():
        with real_session_scope() as session:
            yield session
            raise RuntimeError("injected commit failure")

    with real_session_scope() as session:
        recording = session.get(PlaudFile, "clean")
        audio_path = recording.audio_path
        wav_path = recording.wav_path
    monkeypatch.setattr(cleanup, "session_scope", failing_session_scope)
    with pytest.raises(RuntimeError, match="injected commit failure"):
        if operation == "audio":
            cleanup.remove_local_audio("clean")
        else:
            cleanup.delete_local_processing("clean")

    assert audio_path and wav_path
    assert tmp_path.joinpath("audio", "audio.mp3").exists()
    assert tmp_path.joinpath("audio", "audio.wav").exists()
    assert not list(tmp_path.joinpath("audio").glob(".*.localplaud-delete-*"))
    with real_session_scope() as session:
        recording = session.get(PlaudFile, "clean")
        assert recording.status == FileStatus.done
        assert recording.audio_path == audio_path
        assert recording.wav_path == wav_path
