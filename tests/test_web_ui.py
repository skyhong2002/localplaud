"""Tests for the web UI pages render (dashboard, search, status, detail)."""

from __future__ import annotations


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'ui.db'}")
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed():
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
                        duration_ms=600000, start_time_ms=1783582737000, scene=1))
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
    _seed()
    r = c.get("/file/r1")
    assert r.status_code == 200
    assert "SPEAKER_00" in r.text
    assert 'data-start' in r.text  # seekable segments
    assert "meeting" in r.text.lower()  # summary tab
    assert "Processing details" in r.text
    assert "embedding model unavailable" in r.text
    assert "Resume" in r.text and "Rebuild all" in r.text
    assert ".segedit[hidden] { display:none; }" in r.text
    assert ".sb-group { flex-direction:row; flex:none; }" in r.text
    assert ".doc { max-width:960px" in r.text
    assert 'data-panel="sum-0" class="on"' in r.text
    assert 'data-panel="transcript" class=""' in r.text
    assert 'id="generate-overlay"' in r.text


def test_status_page_renders(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/status")
    assert r.status_code == 200
    assert "Environment" in r.text and "Pipeline" in r.text and "Configuration" in r.text
    assert "Needs attention" in r.text and "embedding model unavailable" in r.text


def test_export_markdown_endpoint(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/file/r1/export.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "Weekly Sync" in r.text and "## Transcript" in r.text
    assert c.get("/file/missing/export.md").status_code == 404


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
    assert "Meeting notes ☁" in detail.text
    assert "Plaud transcript ☁" in detail.text
    assert "local processing remains independent" in detail.text
    assert "imported text" in detail.text


def test_add_audio_explains_and_starts_browser_sync(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    page = c.get("/")

    assert "You never need to press Plaud's Generate button" in page.text
    assert 'id="sync-now"' in page.text
    assert "New files appear in All files and the recent-file sidebar" in page.text
    assert c.get("/api/sync/status").status_code == 200


def test_import_audio_appears_in_library_without_cloud(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)

    response = c.post(
        "/api/files/import",
        files=[("files", ("customer-call.mp3", b"ID3-local-audio", "audio/mpeg"))],
        data={"process": "false"},
    )

    assert response.status_code == 201
    imported = response.json()["files"][0]
    listing = c.get("/api/files").json()["files"]
    assert any(row["id"] == imported["id"] and row["filename"] == "customer-call" for row in listing)
    detail = c.get(f"/file/{imported['id']}")
    assert detail.status_code == 200
    assert "Turn this recording into notes" in detail.text


def test_import_audio_rejects_non_audio_extension(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    response = c.post(
        "/api/files/import",
        files=[("files", ("notes.txt", b"not audio", "text/plain"))],
        data={"process": "false"},
    )
    assert response.status_code == 415
