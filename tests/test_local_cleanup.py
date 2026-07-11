"""Explicit local-only audio and derived-artifact cleanup."""

from __future__ import annotations


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
                Chunk(file_id="clean", idx=0, text="chunk"),
                StageRun(file_id="clean", stage=StageName.index),
                UserNote(file_id="clean", title="Keep me", content_md="user-authored"),
            ]
        )
        session.add(PlaudFile(id="upload", filename="Upload", origin="local"))
    return TestClient(app), audio, wav, waveform


def test_delete_processing_preserves_cloud_and_user_data(monkeypatch, tmp_path):
    client, audio, wav, _waveform = _client(monkeypatch, tmp_path)
    page = client.get("/file/clean")
    assert "Delete local processing" in page.text and "Remove local audio" in page.text
    response = client.delete("/api/files/clean/local-processing")
    assert response.status_code == 200
    assert response.json()["removed"] == {
        "revisions": 1,
        "transcripts": 1,
        "notes": 1,
        "chunks": 1,
        "stages": 1,
    }
    assert audio.exists() and not wav.exists()
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        row = session.get(PlaudFile, "clean")
        assert row.status == FileStatus.downloaded
        assert [item.source for item in row.transcripts] == ["cloud"]
        assert [item.source for item in row.transcript_revisions] == ["cloud"]
        assert [item.source for item in row.summaries] == ["cloud"]
        assert len(row.user_notes) == 1 and row.user_notes[0].title == "Keep me"


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
        assert row.audio_path is None and row.wav_path is None
        assert len(row.transcripts) == 2 and len(row.summaries) == 2
    assert client.delete("/api/files/upload/local-audio").status_code == 409
    assert client.delete("/api/files/missing/local-audio").status_code == 404
