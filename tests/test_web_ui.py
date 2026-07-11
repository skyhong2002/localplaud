"""Tests for the web UI pages render (dashboard, search, status, detail)."""

from __future__ import annotations


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'ui.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed(audio_path: str | None = None):
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Summary,
        Transcript,
    )
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r1", filename="Weekly Sync", status=FileStatus.done,
                        duration_ms=600000, start_time_ms=1783582737000, scene=1,
                        audio_path=audio_path))
        s.add(Transcript(file_id="r1", provider="faster-whisper", language="en", has_speakers=True,
                         text="hi", segments=[{"text": "hello team", "start": 1.0, "end": 2.0, "speaker": "SPEAKER_00"}]))
        s.add(Summary(file_id="r1", template="meeting", title="Sync", content_md="# Sync\n\n- point"))
        s.add(
            StageRun(
                file_id="r1",
                stage=StageName.index,
                status=StageStatus.failed,
                attempts=1,
                error="embedding model unavailable",
            )
        )


def test_dashboard_renders(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/")
    assert r.status_code == 200
    assert "Weekly Sync" in r.text
    assert "Total audio" in r.text  # stat tiles present


def test_detail_page_renders(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    _seed(str(audio))
    r = c.get("/file/r1")
    assert r.status_code == 200
    assert "SPEAKER_00" in r.text
    assert 'data-start' in r.text  # seekable segments
    assert "meeting" in r.text.lower()  # summary tab
    assert "Processing details" in r.text
    assert "embedding model unavailable" in r.text
    assert "Resume" in r.text and "Rebuild all" in r.text
    assert "Execution profile" in r.text and "Current Settings" in r.text


def test_metadata_only_plaud_recording_offers_audio_import(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/file/r1")
    assert r.status_code == 200
    assert "Import audio" in r.text
    assert 'hx-post="/api/files/r1/reprocess"' not in r.text
    assert 'hx-post="/api/files/r1/reprocess?force=true"' not in r.text


def test_recording_profile_picker_persists_override(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import ExecutionProfile, RecordingProfileOverride
    from localplaud.db.session import session_scope

    with session_scope() as session:
        profile_id = session.query(ExecutionProfile.id).filter_by(is_system_default=True).scalar()
    response = c.post("/file/r1/profile", data={"profile_id": profile_id}, follow_redirects=False)
    assert response.status_code == 303
    with session_scope() as session:
        assert session.get(RecordingProfileOverride, "r1").profile_id == profile_id


def test_status_page_renders(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/status")
    assert r.status_code == 200
    assert "Environment" in r.text and "Pipeline" in r.text and "Configuration" in r.text
    assert "Needs attention" in r.text and "embedding model unavailable" in r.text


def test_settings_editor_renders_models_and_profile_builder(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.get("/settings")
    assert r.status_code == 200
    assert "Model catalog" in r.text
    assert "Add model" in r.text
    assert "Create execution profile" in r.text
    assert "Local only / no egress" in r.text
    assert "New version" in r.text and "Edit" in r.text and "Delete" in r.text
    assert "Remote workers" in r.text and "Register worker" in r.text


def test_export_markdown_endpoint(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/file/r1/export.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "Weekly Sync" in r.text and "## Transcript" in r.text
    assert c.get("/file/missing/export.md").status_code == 404


def test_export_menu_and_format_endpoints(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    _seed(str(audio))
    page = c.get("/file/r1")
    assert "Export recording" in page.text
    assert "Speaker labels" in page.text and "Original audio" in page.text
    txt = c.get("/file/r1/export/transcript.txt?timestamps=false&speakers=false")
    assert txt.status_code == 200 and "hello team" in txt.text
    assert "SPEAKER_00" not in txt.text and "[00:01]" not in txt.text
    assert c.get("/file/r1/export/transcript.srt").status_code == 200
    assert c.get("/file/r1/export/transcript.docx").content.startswith(b"PK")
    assert c.get("/file/r1/export/transcript.pdf").content.startswith(b"%PDF")
    assert c.get("/file/r1/export/notes.txt").status_code == 200
    assert c.get("/file/r1/export/audio").content == b"audio"


def test_reprocess_missing_audio(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()  # r1 has no audio_path
    assert c.post("/file/r1/reprocess").status_code == 400


def test_search_page_renders_empty(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.get("/search")
    assert r.status_code == 200
    # a query with no index / provider shouldn't 500
    assert c.get("/search?q=anything").status_code == 200


def test_independent_ui_labels_imported_transcript_without_treating_it_as_local(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, Summary, Transcript
    from localplaud.db.session import session_scope

    with session_scope() as s:
        file = PlaudFile(id="cloud", filename="Cloud import", status=FileStatus.downloaded)
        file.transcripts = [
            Transcript(
                provider="plaud",
                source="cloud",
                text="imported text",
                segments=[{"text": "imported text", "start": 0.0, "end": 1.0}],
            )
        ]
        file.summaries = [Summary(template="plaud", source="cloud", content_md="note")]
        s.add(file)

    listing = c.get("/api/files").json()["files"][0]
    assert listing["has_transcript"] is False
    assert listing["has_imported_transcript"] is True
    assert listing["has_summary"] is False
    assert listing["has_imported_summary"] is True

    detail = c.get("/file/cloud")
    assert detail.status_code == 200
    assert "Plaud import" in detail.text
    assert "canonical result" in detail.text
    assert "imported text" in detail.text
