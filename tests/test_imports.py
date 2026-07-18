"""Metadata-first Plaud import and explicit audio import tests."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager

import httpx
import respx


def _reset_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'imports.db'}")
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    return get_settings(reload=True)


def test_metadata_import_mirrors_artifacts_without_downloading(monkeypatch, tmp_path):
    settings = _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, ImportRun, PlaudFile, Summary, Transcript
    from localplaud.db.session import init_db, session_scope
    from localplaud.imports import _run_plaud_metadata_import
    from localplaud.plaud.models import PlaudFileDTO

    init_db()

    class FakeClient:
        downloads = 0
        detail_calls: list[str] = []
        files = [
            PlaudFileDTO(id="p1", filename="Cloud meeting", duration=42_000),
            PlaudFileDTO(id="p2", filename="No intelligence"),
        ]

        def iter_files(self, include_trash=False):
            yield from self.files

        def get_detail(self, file_id):
            self.detail_calls.append(file_id)
            return {"id": file_id}

        def get_cloud_notes(self, file_id, detail):
            if file_id != "p1":
                return []
            return [
                {
                    "key": "auto_sum_note",
                    "title": "Paid summary",
                    "markdown": "# Paid summary\n\n- Decision",
                },
                {
                    "key": "action_items",
                    "title": "Actions",
                    "markdown": "# Actions\n\n- Follow up",
                },
            ]

        def get_cloud_transcript_segments(self, file_id, detail):
            if file_id == "p1":
                return [{"text": "Hello", "start": 0.0, "end": 1.0, "speaker": "A"}]
            return []

        def download_audio(self, *_args):
            self.downloads += 1
            raise AssertionError("metadata import must not download audio")

    fake = FakeClient()

    @contextmanager
    def fake_factory(_cfg):
        yield fake

    monkeypatch.setattr("localplaud.imports.make_plaud_client", fake_factory)
    with session_scope() as session:
        session.add(ImportRun(id="run", source="plaud", status="queued"))
    _run_plaud_metadata_import("run", settings)

    with session_scope() as session:
        p1 = session.get(PlaudFile, "p1")
        p2 = session.get(PlaudFile, "p2")
        assert p1.status == FileStatus.metadata_only
        assert p1.audio_path is None
        assert p1.origin == "plaud"
        assert p1.cloud_artifacts_synced_at is not None
        assert p2.cloud_artifacts_synced_at is not None
        assert session.query(Transcript).filter_by(file_id="p1", source="cloud").count() == 1
        notes = (
            session.query(Summary)
            .filter_by(file_id="p1", source="cloud")
            .order_by(Summary.template)
            .all()
        )
        assert [(note.template, note.title) for note in notes] == [
            ("action_items", "Actions"),
            ("auto_sum_note", "Paid summary"),
        ]
        run = session.get(ImportRun, "run")
        assert (run.status, run.total, run.processed, run.transcript_count, run.summary_count) == (
            "completed", 2, 2, 1, 1
        )
        assert run.skipped_count == 0
    assert fake.detail_calls == ["p1", "p2"]
    assert fake.downloads == 0

    import localplaud.imports as imports

    def run_import(run_id):
        with session_scope() as session:
            session.add(ImportRun(id=run_id, source="plaud", status="queued"))
        _run_plaud_metadata_import(run_id, settings)

    refresh_calls: list[str] = []

    def counted_refresh(_client, file_id):
        refresh_calls.append(file_id)
        return (file_id == "p1", file_id == "p1")

    monkeypatch.setattr(imports, "refresh_cloud_artifacts_for", counted_refresh)
    run_import("unchanged")
    assert refresh_calls == []
    with session_scope() as session:
        run = session.get(ImportRun, "unchanged")
        assert (run.skipped_count, run.total, run.transcript_count, run.summary_count) == (
            2,
            2,
            1,
            1,
        )
        assert imports.import_run_to_dict(run)["skipped"] == 2

    fake.files[0] = PlaudFileDTO(
        id="p1", filename="Cloud meeting renamed", duration=42_000
    )
    run_import("changed")
    assert refresh_calls == ["p1"]
    with session_scope() as session:
        run = session.get(ImportRun, "changed")
        assert (run.changed_count, run.skipped_count) == (1, 1)

    fake.files[1] = PlaudFileDTO(id="p2", filename="No intelligence renamed")
    failed_calls: list[str] = []

    def failed_refresh(_client, file_id):
        failed_calls.append(file_id)
        raise RuntimeError("detail unavailable")

    monkeypatch.setattr(imports, "refresh_cloud_artifacts_for", failed_refresh)
    run_import("failed-refresh")
    assert failed_calls == ["p2"]
    with session_scope() as session:
        assert session.get(PlaudFile, "p2").cloud_artifacts_synced_at is None
        run = session.get(ImportRun, "failed-refresh")
        assert (run.failed_count, run.skipped_count) == (1, 1)

    refresh_calls.clear()
    monkeypatch.setattr(imports, "refresh_cloud_artifacts_for", counted_refresh)
    run_import("retry-refresh")
    assert refresh_calls == ["p2"]
    with session_scope() as session:
        assert session.get(PlaudFile, "p2").cloud_artifacts_synced_at is not None
        assert session.get(ImportRun, "retry-refresh").skipped_count == 1


def test_recording_cloud_refresh_imports_all_notes_and_stamps_sync(
    monkeypatch, tmp_path
):
    _reset_db(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient

    from localplaud.api.app import app
    from localplaud.db.models import FileStatus, PlaudFile, Summary
    from localplaud.db.session import init_db, session_scope

    init_db()
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id="plaud-file",
                    filename="Plaud file",
                    origin="plaud",
                    status=FileStatus.metadata_only,
                ),
                PlaudFile(
                    id="local-file",
                    filename="Local file",
                    origin="local",
                    status=FileStatus.downloaded,
                ),
                PlaudFile(
                    id="trashed-file",
                    filename="Trashed file",
                    origin="plaud",
                    is_trash=True,
                    status=FileStatus.metadata_only,
                ),
            ]
        )

    class FakeClient:
        def get_detail(self, file_id):
            return {"id": file_id}

        def get_cloud_notes(self, file_id, detail):
            return [
                {"key": "summary", "title": "Summary", "markdown": "# Summary"},
                {"key": "actions", "title": "Actions", "markdown": "# Actions"},
            ]

        def get_cloud_transcript_segments(self, file_id, detail):
            return [{"text": "Cloud words", "start": 0.0, "end": 1.0}]

    @contextmanager
    def fake_factory(_cfg):
        yield FakeClient()

    monkeypatch.setattr("localplaud.plaud.make_plaud_client", fake_factory)
    client = TestClient(app)
    response = client.post("/api/files/plaud-file/refresh-cloud-artifacts")
    assert response.status_code == 200
    assert response.json() == {"transcript": True, "notes": 2}
    assert client.post(
        "/api/files/local-file/refresh-cloud-artifacts"
    ).status_code == 422
    assert client.post(
        "/api/files/trashed-file/refresh-cloud-artifacts"
    ).status_code == 404
    with session_scope() as session:
        row = session.get(PlaudFile, "plaud-file")
        assert row.cloud_artifacts_synced_at is not None
        assert {
            (note.template, note.title, note.source)
            for note in session.query(Summary).filter_by(file_id="plaud-file")
        } == {
            ("summary", "Summary", "cloud"),
            ("actions", "Actions", "cloud"),
        }


def test_recording_cloud_refresh_redacts_provider_error(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient

    from localplaud.api.app import app
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope

    init_db()
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="plaud-file",
                filename="Plaud file",
                origin="plaud",
                status=FileStatus.metadata_only,
            )
        )

    @contextmanager
    def failed_factory(_cfg):
        raise RuntimeError("refresh failed token=secret-value")
        yield

    monkeypatch.setattr("localplaud.plaud.make_plaud_client", failed_factory)
    response = TestClient(app).post("/api/files/plaud-file/refresh-cloud-artifacts")
    assert response.status_code == 502
    assert response.json()["detail"] == "refresh failed token=[REDACTED]"


@respx.mock
def test_cloud_note_assets_are_mirrored_served_and_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALPLAUD_API__AUTH_TOKEN", "note-asset-token")
    settings = _reset_db(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient

    from localplaud.api.app import app
    from localplaud.db.models import FileStatus, PlaudFile, Summary
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import refresh_cloud_artifacts_for

    init_db()
    file_id = "asset-file"
    relative_path = "permanent/poster%20image.png"
    signed_url = "https://assets.example/signed/poster.png?token=secret"
    image = b"fake-png-bytes"
    name = f"{hashlib.sha256(image).hexdigest()[:16]}.png"
    with session_scope() as session:
        session.add(
            PlaudFile(
                id=file_id,
                filename="Asset note",
                origin="plaud",
                status=FileStatus.metadata_only,
            )
        )

    class FakeClient:
        def get_detail(self, requested_file_id):
            return {"id": requested_file_id}

        def get_cloud_notes(self, requested_file_id, detail):
            return [
                {
                    "key": "auto_sum_note",
                    "title": "Summary",
                    "markdown": f"Before\n\n![Poster]({relative_path})\n\nAfter",
                    "assets": {
                        "permanent/poster image.png": signed_url,
                    },
                }
            ]

        def get_cloud_transcript_segments(self, requested_file_id, detail):
            return []

    safe_urls = []
    monkeypatch.setattr(
        "localplaud.poller.poll._assert_safe_fetch_url", lambda url: safe_urls.append(url)
    )
    route = respx.get(signed_url).mock(return_value=httpx.Response(200, content=image))
    assert refresh_cloud_artifacts_for(FakeClient(), file_id) == (False, True)
    assert refresh_cloud_artifacts_for(FakeClient(), file_id) == (False, True)

    asset_path = settings.poller.download_dir / file_id / "note-assets" / name
    assert asset_path.read_bytes() == image
    assert list(asset_path.parent.iterdir()) == [asset_path]
    with session_scope() as session:
        note = session.query(Summary).filter_by(file_id=file_id).one()
        assert note.content_md == (
            f"Before\n\n![Poster](/api/files/{file_id}/note-assets/{name})\n\nAfter"
        )

    assert TestClient(app).get(f"/api/files/{file_id}/note-assets/{name}").status_code == 401
    client = TestClient(app, headers={"x-auth-token": "note-asset-token"})
    response = client.get(f"/api/files/{file_id}/note-assets/{name}")
    assert response.status_code == 200
    assert response.content == image
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, max-age=86400"
    assert client.get(
        f"/api/files/{file_id}/note-assets/../../secret.png"
    ).status_code == 404
    assert safe_urls == [signed_url, signed_url]
    assert route.call_count == 2


@respx.mock
def test_cloud_note_asset_failure_keeps_import_successful(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, Summary
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import refresh_cloud_artifacts_for

    init_db()
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="failed-asset",
                filename="Failed asset",
                origin="plaud",
                status=FileStatus.metadata_only,
            )
        )

    class FakeClient:
        def get_detail(self, file_id):
            return {"id": file_id}

        def get_cloud_notes(self, file_id, detail):
            return [
                {
                    "key": "auto_sum_note",
                    "title": "Summary",
                    "markdown": "Before\n\n![Poster](temporary/poster.png)\n\nAfter",
                    "assets": {"temporary/poster.png": "https://assets.example/missing.png"},
                }
            ]

        def get_cloud_transcript_segments(self, file_id, detail):
            return []

    monkeypatch.setattr("localplaud.poller.poll._assert_safe_fetch_url", lambda url: None)
    respx.get("https://assets.example/missing.png").mock(return_value=httpx.Response(503))
    assert refresh_cloud_artifacts_for(FakeClient(), "failed-asset") == (False, True)
    with session_scope() as session:
        note = session.query(Summary).filter_by(file_id="failed-asset").one()
        assert note.content_md == "Before\n\nPoster\n\nAfter"


def test_incremental_import_migration_is_additive_and_idempotent(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from localplaud.db.migrations import migrate_incremental_import_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-import.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE import_runs (id VARCHAR(36) PRIMARY KEY)"))

    assert set(migrate_incremental_import_schema(engine)) == {
        "plaud_files.cloud_artifacts_synced_at",
        "import_runs.skipped_count",
    }
    assert migrate_incremental_import_schema(engine) == []
    plaud_columns = {
        column["name"]: column for column in inspect(engine).get_columns("plaud_files")
    }
    import_columns = {
        column["name"]: column for column in inspect(engine).get_columns("import_runs")
    }
    assert plaud_columns["cloud_artifacts_synced_at"]["nullable"] is True
    assert import_columns["skipped_count"]["nullable"] is False
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO import_runs (id) VALUES ('legacy-run')"))
        assert connection.execute(
            text("SELECT skipped_count FROM import_runs WHERE id = 'legacy-run'")
        ).scalar_one() == 0


def test_interrupted_import_recovery_explains_incremental_restart(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import ImportRun
    from localplaud.db.session import init_db, session_scope
    from localplaud.i18n import translator
    from localplaud.imports import recover_interrupted_imports

    init_db()
    with session_scope() as session:
        session.add(ImportRun(id="interrupted", source="plaud", status="running"))

    assert recover_interrupted_imports() == 1
    with session_scope() as session:
        run = session.get(ImportRun, "interrupted")
        assert run.status == "failed"
        assert "already-synced recordings will be skipped" in run.error
        assert translator("zh-Hant-TW")(run.error).startswith("應用程式重新啟動")


def test_local_audio_upload_and_metadata_only_ui(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient

    from localplaud.api.app import app
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import init_db, session_scope

    init_db()
    client = TestClient(app)
    page = client.get("/")
    assert "Add audio" in page.text and "Import from Plaud" in page.text
    response = client.post(
        "/api/imports/local/audio",
        files={"file": ("interview.mp3", b"not-a-real-mp3", "audio/mpeg")},
    )
    assert response.status_code == 201
    with session_scope() as session:
        row = session.get(PlaudFile, response.json()["id"])
        assert row.origin == "local"
        assert row.audio_path and row.filename == "interview"
    assert client.post(
        "/api/imports/local/audio", files={"file": ("bad.exe", b"x")}
    ).status_code == 415


def test_on_demand_plaud_audio_import_uses_its_existing_claim(monkeypatch, tmp_path):
    settings = _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.imports import _run_audio_import

    init_db()
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="history-audio",
                filename="History audio",
                origin="plaud",
                status=FileStatus.downloading,
                raw={"id": "history-audio", "filename": "History audio"},
            )
        )

    class FakeClient:
        calls = 0

        def download_audio(self, dto, destination):
            self.calls += 1
            path = destination / "audio.opus"
            path.write_bytes(b"audio")
            return path

    fake = FakeClient()

    @contextmanager
    def fake_factory(_config):
        yield fake

    monkeypatch.setattr("localplaud.imports.make_plaud_client", fake_factory)
    _run_audio_import("history-audio", {"id": "history-audio"}, settings)

    assert fake.calls == 1
    with session_scope() as session:
        row = session.get(PlaudFile, "history-audio")
        assert row.status == FileStatus.downloaded
        assert row.audio_path


def test_poller_baselines_history_then_queues_only_new_recordings(monkeypatch, tmp_path):
    settings = _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, KeyValue, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.plaud.models import PlaudFileDTO
    from localplaud.poller.poll import sync_file_list

    init_db()
    assert settings.poller.auto_download is True

    class FakeClient:
        files = [PlaudFileDTO(id="historical", filename="Historical")]

        def iter_files(self, include_trash=False):
            yield from self.files

    client = FakeClient()
    assert sync_file_list(client, settings) == (1, 0)
    with session_scope() as session:
        assert session.get(PlaudFile, "historical").status == FileStatus.metadata_only
        assert session.get(KeyValue, "plaud_catalog_baseline_v1") is not None

    client.files.append(PlaudFileDTO(id="new-upload", filename="New upload"))
    assert sync_file_list(client, settings) == (1, 0)
    with session_scope() as session:
        assert session.get(PlaudFile, "historical").status == FileStatus.metadata_only
        assert session.get(PlaudFile, "new-upload").status == FileStatus.discovered


def test_disabled_auto_download_keeps_post_baseline_recordings_metadata_only(
    monkeypatch, tmp_path
):
    settings = _reset_db(monkeypatch, tmp_path)
    settings.poller.auto_download = False
    from localplaud.db.models import FileStatus, KeyValue, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.plaud.models import PlaudFileDTO
    from localplaud.poller.poll import sync_file_list

    init_db()
    with session_scope() as session:
        session.add(KeyValue(key="plaud_catalog_baseline_v1", value={}))

    class FakeClient:
        def iter_files(self, include_trash=False):
            yield PlaudFileDTO(id="manual", filename="Manual import")

    assert sync_file_list(FakeClient(), settings) == (1, 0)
    with session_scope() as session:
        assert session.get(PlaudFile, "manual").status == FileStatus.metadata_only


def test_empty_catalog_baseline_queues_the_first_future_recording(monkeypatch, tmp_path):
    settings = _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.plaud.models import PlaudFileDTO
    from localplaud.poller.poll import sync_file_list

    init_db()

    class FakeClient:
        files: list[PlaudFileDTO] = []

        def iter_files(self, include_trash=False):
            yield from self.files

    client = FakeClient()
    assert sync_file_list(client, settings) == (0, 0)
    client.files.append(PlaudFileDTO(id="first-upload", filename="First upload"))
    assert sync_file_list(client, settings) == (1, 0)
    with session_scope() as session:
        assert session.get(PlaudFile, "first-upload").status == FileStatus.discovered


def test_existing_catalog_without_marker_gets_one_safe_upgrade_baseline(
    monkeypatch, tmp_path
):
    settings = _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, KeyValue, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.plaud.models import PlaudFileDTO
    from localplaud.poller.poll import sync_file_list

    init_db()
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="mirrored-history",
                filename="Mirrored history",
                origin="plaud",
                status=FileStatus.metadata_only,
            )
        )
        session.add(
            PlaudFile(
                id="queued-history",
                filename="Queued history",
                origin="plaud",
                status=FileStatus.discovered,
            )
        )
        session.add(
            PlaudFile(
                id="failed-history",
                filename="Failed history",
                origin="plaud",
                status=FileStatus.error,
                error="old download failure",
            )
        )

    class FakeClient:
        files = [
            PlaudFileDTO(id="mirrored-history", filename="Mirrored history"),
            PlaudFileDTO(id="queued-history", filename="Queued history"),
            PlaudFileDTO(id="failed-history", filename="Failed history"),
            PlaudFileDTO(id="found-during-upgrade", filename="Found during upgrade"),
        ]

        def iter_files(self, include_trash=False):
            yield from self.files

    client = FakeClient()
    assert sync_file_list(client, settings) == (1, 0)
    with session_scope() as session:
        assert session.get(PlaudFile, "mirrored-history").status == FileStatus.metadata_only
        assert session.get(PlaudFile, "queued-history").status == FileStatus.metadata_only
        failed = session.get(PlaudFile, "failed-history")
        assert failed.status == FileStatus.metadata_only and failed.error is None
        assert session.get(PlaudFile, "found-during-upgrade").status == FileStatus.metadata_only
        assert session.get(KeyValue, "plaud_catalog_baseline_v1") is not None

    client.files.append(PlaudFileDTO(id="post-upgrade", filename="Post-upgrade upload"))
    assert sync_file_list(client, settings) == (1, 0)
    with session_scope() as session:
        assert session.get(PlaudFile, "post-upgrade").status == FileStatus.discovered
