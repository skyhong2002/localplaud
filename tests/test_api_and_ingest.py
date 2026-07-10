"""Tests for audio serving and cloud-summary ingestion."""

from __future__ import annotations

import gzip

import httpx
import respx

API = "https://api-apse1.plaud.ai"


def _reset_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)


def test_audio_route_serves_and_404(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    _reset_db(monkeypatch, tmp_path)
    from localplaud.api.app import app
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope

    init_db()
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"ID3fakeaudio")
    with session_scope() as s:
        s.add(PlaudFile(id="f1", filename="rec", status=FileStatus.done, audio_path=str(audio)))
        s.add(PlaudFile(id="f2", filename="noaudio", status=FileStatus.discovered))

    client = TestClient(app)
    r = client.get("/audio/f1")
    assert r.status_code == 200
    assert r.content == b"ID3fakeaudio"
    assert r.headers["content-type"] == "audio/mpeg"
    assert client.get("/audio/f2").status_code == 404
    assert client.get("/audio/nope").status_code == 404


@respx.mock
def test_ingest_cloud_summaries(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    monkeypatch.setattr("localplaud.plaud.client._assert_safe_fetch_url", lambda u: None)
    from localplaud.config import get_settings
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.plaud.client import PlaudClient
    from localplaud.poller.poll import ingest_cloud_summaries

    init_db()
    with session_scope() as s:
        s.add(PlaudFile(id="fc", filename="rec", cloud_is_summary=True))
        s.add(PlaudFile(id="fn", filename="no-cloud", cloud_is_summary=False))

    asset = "https://apse1-prod-plaud-content-storage.s3.amazonaws.com/permanent/w/m/file_summary/fc/ai_content.md.gz?Signature=z"
    respx.get(f"{API}/file/detail/fc").mock(
        return_value=httpx.Response(200, json={"status": 0, "data": {"summary": asset}})
    )
    respx.get(url__regex=r".*ai_content\.md\.gz.*").mock(
        return_value=httpx.Response(200, content=gzip.compress(b"# Cloud Note\n\nbody"))
    )

    settings = get_settings()
    from localplaud.config import PlaudConfig

    settings.plaud = PlaudConfig(api_base=API, token="Bearer t")
    with PlaudClient(settings.plaud) as c:
        n = ingest_cloud_summaries(c, settings)
    assert n == 1
    with session_scope() as s:
        summaries = s.get(PlaudFile, "fc").summaries
        assert len(summaries) == 1
        assert summaries[0].source == "cloud"
        assert summaries[0].template == "plaud"
        assert summaries[0].title == "Cloud Note"
