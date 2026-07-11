"""Durable local recording-title overrides across sync and user surfaces."""

from __future__ import annotations


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'titles.db'}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    settings = get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.models import FileStatus, PlaudFile, Transcript
    from localplaud.db.session import init_db, session_scope

    init_db()
    with session_scope() as session:
        session.add(
            PlaudFile(id="title", filename="Plaud cloud title", status=FileStatus.done)
        )
        session.add(
            Transcript(
                file_id="title",
                provider="seed",
                source="local",
                text="hello",
                segments=[{"text": "hello", "start": 0.0, "end": 1.0}],
            )
        )
    return TestClient(app), settings


def test_local_title_survives_cloud_rename_and_drives_surfaces(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    renamed = client.patch(
        "/api/files/title/title", json={"title": "  My durable title  "}
    )
    assert renamed.status_code == 200
    assert renamed.json() == {
        "file_id": "title",
        "title": "My durable title",
        "local_title": "My durable title",
        "cloud_title": "Plaud cloud title",
    }
    listing = client.get("/api/files?q=durable").json()["files"]
    assert listing[0]["filename"] == "My durable title"
    assert listing[0]["cloud_filename"] == "Plaud cloud title"
    detail = client.get("/file/title")
    assert "My durable title" in detail.text
    assert "Plaud title: Plaud cloud title" in detail.text
    assert "# My durable title" in client.get("/file/title/export.md").text

    from localplaud.plaud.models import PlaudFileDTO
    from localplaud.poller.poll import sync_file_list

    class FakeClient:
        def iter_files(self, include_trash=False):
            yield PlaudFileDTO(id="title", filename="New Plaud title")

    sync_file_list(FakeClient(), settings)
    refreshed = client.get("/api/files").json()["files"][0]
    assert refreshed["filename"] == "My durable title"
    assert refreshed["cloud_filename"] == "New Plaud title"


def test_clear_title_returns_to_latest_cloud_title(monkeypatch, tmp_path):
    client, _settings = _client(monkeypatch, tmp_path)
    assert client.patch("/api/files/title/title", json={"title": "Local"}).status_code == 200
    cleared = client.patch("/api/files/title/title", json={"title": "   "})
    assert cleared.status_code == 200
    assert cleared.json()["local_title"] is None
    assert cleared.json()["title"] == "Plaud cloud title"
    assert client.patch("/api/files/missing/title", json={"title": "x"}).status_code == 404
