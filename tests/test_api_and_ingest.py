"""Tests for serving locally owned recording audio."""

from __future__ import annotations


def test_audio_route_serves_and_404(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)

    from localplaud.api.app import app
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope

    init_db()
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"ID3fakeaudio")
    with session_scope() as session:
        session.add(
            PlaudFile(id="f1", filename="rec", status=FileStatus.done, audio_path=str(audio))
        )
        session.add(PlaudFile(id="f2", filename="noaudio", status=FileStatus.discovered))

    client = TestClient(app)
    response = client.get("/audio/f1")
    assert response.status_code == 200
    assert response.content == b"ID3fakeaudio"
    assert response.headers["content-type"] == "audio/mpeg"
    assert client.get("/audio/f2").status_code == 404
    assert client.get("/audio/nope").status_code == 404
