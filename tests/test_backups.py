from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from io import BytesIO

import httpx
import respx


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    database = tmp_path / "data" / "localplaud.db"
    media = tmp_path / "media"
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(media))
    monkeypatch.setenv("LOCALPLAUD_PLAUD__OFFICIAL__TOKENS_PATH", str(tmp_path / "tokens.json"))
    monkeypatch.setenv("BACKUP_TEST_SECRET", "must-not-enter-backup")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app

    return TestClient(app), database, media


def test_database_and_media_backups_are_consistent_private_and_downloadable(
    monkeypatch, tmp_path
):
    client, _database, media = _client(monkeypatch, tmp_path)
    with client:
        from localplaud.db.models import KeyValue, PlaudFile
        from localplaud.db.session import session_scope

        with session_scope() as session:
            session.add(PlaudFile(id="backup-file", filename="Private meeting"))
            session.add(KeyValue(key="secret-reference", value={"ref": "env:BACKUP_TEST_SECRET"}))
        recording_dir = media / "backup-file"
        recording_dir.mkdir(parents=True)
        (recording_dir / "audio.opus").write_bytes(b"owned audio")
        (recording_dir / "notes.txt").write_text("local notes", encoding="utf-8")
        outside = tmp_path / ".env"
        outside.write_text("BACKUP_TEST_SECRET=must-not-enter-backup", encoding="utf-8")
        (recording_dir / "outside-link").symlink_to(outside)

        database_only = client.post("/api/backups")
        assert database_only.status_code == 201
        first = database_only.json()
        assert first["schema"] == "localplaud-workspace-backup/v1"
        assert first["media"] == {"included": False, "root": None, "files": 0, "bytes": 0}
        assert len(first["sha256"]) == 64

        downloaded = client.get(f"/api/backups/{first['name']}/download")
        assert downloaded.status_code == 200
        assert hashlib.sha256(downloaded.content).hexdigest() == first["sha256"]
        assert b"must-not-enter-backup" not in downloaded.content
        with zipfile.ZipFile(BytesIO(downloaded.content)) as archive:
            assert set(archive.namelist()) == {"database/localplaud.db", "manifest.json"}
            manifest = json.loads(archive.read("manifest.json"))
            assert "Plaud OAuth token files" in manifest["excluded"]
            snapshot_path = tmp_path / "snapshot.db"
            snapshot_path.write_bytes(archive.read("database/localplaud.db"))
        with sqlite3.connect(snapshot_path) as connection:
            assert connection.execute(
                "SELECT filename FROM plaud_files WHERE id='backup-file'"
            ).fetchone() == ("Private meeting",)
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)

        full = client.post("/api/backups?include_media=true")
        assert full.status_code == 201
        assert full.json()["media"]["files"] == 2
        full_download = client.get(f"/api/backups/{full.json()['name']}/download")
        with zipfile.ZipFile(BytesIO(full_download.content)) as archive:
            names = set(archive.namelist())
            assert "media/backup-file/audio.opus" in names
            assert "media/backup-file/notes.txt" in names
            assert "media/backup-file/outside-link" not in names
            assert ".env" not in " ".join(names)

        listed = client.get("/api/backups")
        assert listed.status_code == 200
        assert len(listed.json()["backups"]) == 2
        assert {item["status"] for item in listed.json()["backups"]} == {"ready"}
        settings_page = client.get("/settings")
        assert 'id="private-backup"' in settings_page.text
        assert 'href="#private-backup"' in settings_page.text
        assert "Back up database + media" in settings_page.text
        assert first["name"] in settings_page.text

        assert client.get("/api/backups/../../.env/download").status_code == 404
        removed = client.delete(f"/api/backups/{first['name']}")
        assert removed.status_code == 200 and removed.json() == {"deleted": True}
        assert client.get(f"/api/backups/{first['name']}/download").status_code == 404


def test_authorized_backup_sync_is_durable_idempotent_and_revocable(monkeypatch, tmp_path):
    client, _database, _media = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("BACKUP_SYNC_TOKEN", "remote-backup-secret")
    with client:
        backup = client.post("/api/backups").json()
        base_url = "http://127.0.0.1:9988/webdav/localplaud"
        body = {
            "name": "Private NAS",
            "url": base_url,
            "secret_ref": "env:BACKUP_SYNC_TOKEN",
            "enabled": True,
            "allow_private_network": True,
        }
        denied = client.post(
            "/api/backups/destinations",
            json=body | {"allow_private_network": False},
        )
        assert denied.status_code == 422
        assert "require HTTPS" in denied.json()["detail"]
        assert client.post(
            "/api/backups/destinations",
            json=body | {"url": "http://user:password@127.0.0.1/private"},
        ).status_code == 422
        assert client.post(
            "/api/backups/destinations",
            json=body | {"url": "http://127.0.0.1/private?token=inline"},
        ).status_code == 422

        created = client.post("/api/backups/destinations", json=body)
        assert created.status_code == 201
        destination = created.json()
        assert destination["secret_ref"] == "env:BACKUP_SYNC_TOKEN"
        upload_url = f"{base_url}/{backup['name']}"
        with respx.mock(assert_all_called=False) as mock:
            options = mock.options(base_url).mock(return_value=httpx.Response(204))
            put = mock.put(upload_url).mock(
                side_effect=[
                    httpx.Response(503, text="storage unavailable"),
                    httpx.Response(201, text="stored"),
                ]
            )
            health = client.post(
                f"/api/backups/destinations/{destination['id']}/test"
            )
            assert health.status_code == 200
            assert health.json()["status"] == "healthy"
            assert options.calls[0].request.content == b""

            failed = client.post(
                f"/api/backups/{backup['name']}/sync/{destination['id']}"
            )
            assert failed.status_code == 502
            deliveries = client.get("/api/backups/sync-deliveries").json()["deliveries"]
            assert len(deliveries) == 1
            delivery = deliveries[0]
            assert delivery["status"] == "failed"
            assert delivery["attempt_count"] == 1
            assert delivery["response_status"] == 503
            assert "HTTP 503" in delivery["error"]

            retried = client.post(
                f"/api/backups/sync-deliveries/{delivery['id']}/retry"
            )
            assert retried.status_code == 200
            assert retried.json()["status"] == "completed"
            assert retried.json()["attempt_count"] == 2
            first_request, second_request = put.calls[0].request, put.calls[1].request
            assert first_request.headers["authorization"] == "Bearer remote-backup-secret"
            assert first_request.headers["x-localplaud-backup-sha256"] == backup["sha256"]
            assert first_request.headers["x-localplaud-delivery-id"] == second_request.headers[
                "x-localplaud-delivery-id"
            ]
            assert first_request.content == second_request.content
            assert hashlib.sha256(first_request.content).hexdigest() == backup["sha256"]

            completed_again = client.post(
                f"/api/backups/{backup['name']}/sync/{destination['id']}"
            )
            assert completed_again.status_code == 200
            assert put.call_count == 2

        from localplaud.db.models import BackupSyncDelivery
        from localplaud.db.session import session_scope

        with session_scope() as session:
            row = session.query(BackupSyncDelivery).one()
            assert row.attempt_count == 2
            assert "remote-backup-secret" not in str(row.destination_snapshot)
            assert "remote-backup-secret" not in str(row.__dict__)

        settings_page = client.get("/settings")
        assert "Authorized backup destinations" in settings_page.text
        assert "Private NAS" in settings_page.text
        assert "Upload" in settings_page.text
        assert "Sync history" in settings_page.text
        assert "remote-backup-secret" not in settings_page.text

        assert client.delete(
            f"/api/backups/destinations/{destination['id']}"
        ).status_code == 204
        with session_scope() as session:
            assert session.query(BackupSyncDelivery).one().destination_id is None
        revoked_retry = client.post(
            f"/api/backups/sync-deliveries/{delivery['id']}/retry"
        )
        assert revoked_retry.status_code == 422
        assert "revoked" in revoked_retry.json()["detail"]
