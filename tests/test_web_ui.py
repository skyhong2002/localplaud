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


def _seed():
    from localplaud.db.models import FileStatus, PlaudFile, Summary, Transcript
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r1", filename="Weekly Sync", status=FileStatus.done,
                        duration_ms=600000, start_time_ms=1783582737000, scene=1))
        s.add(Transcript(file_id="r1", provider="faster-whisper", language="en", has_speakers=True,
                         text="hi", segments=[{"text": "hello team", "start": 1.0, "end": 2.0, "speaker": "SPEAKER_00"}]))
        s.add(Summary(file_id="r1", template="meeting", title="Sync", content_md="# Sync\n\n- point"))


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


def test_status_page_renders(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.get("/status")
    assert r.status_code == 200
    assert "Environment" in r.text and "Pipeline" in r.text and "Configuration" in r.text


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
