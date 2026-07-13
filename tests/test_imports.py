"""Metadata-first Plaud import and explicit audio import tests."""

from __future__ import annotations

from contextlib import contextmanager


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

        def iter_files(self, include_trash=False):
            yield PlaudFileDTO(id="p1", filename="Cloud meeting", duration=42_000)
            yield PlaudFileDTO(id="p2", filename="No intelligence")

        def get_detail(self, file_id):
            return {"id": file_id}

        def get_cloud_summary_md(self, file_id, detail):
            return "# Paid summary\n\n- Decision" if file_id == "p1" else None

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
        assert session.get(PlaudFile, "p1").status == FileStatus.metadata_only
        assert session.get(PlaudFile, "p1").audio_path is None
        assert session.get(PlaudFile, "p1").origin == "plaud"
        assert session.query(Transcript).filter_by(file_id="p1", source="cloud").count() == 1
        assert session.query(Summary).filter_by(file_id="p1", source="cloud").count() == 1
        run = session.get(ImportRun, "run")
        assert (run.status, run.total, run.processed, run.transcript_count, run.summary_count) == (
            "completed", 2, 2, 1, 1
        )
    assert fake.downloads == 0


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

    class FakeClient:
        files = [
            PlaudFileDTO(id="mirrored-history", filename="Mirrored history"),
            PlaudFileDTO(id="found-during-upgrade", filename="Found during upgrade"),
        ]

        def iter_files(self, include_trash=False):
            yield from self.files

    client = FakeClient()
    assert sync_file_list(client, settings) == (1, 0)
    with session_scope() as session:
        assert session.get(PlaudFile, "mirrored-history").status == FileStatus.metadata_only
        assert session.get(PlaudFile, "found-during-upgrade").status == FileStatus.metadata_only
        assert session.get(KeyValue, "plaud_catalog_baseline_v1") is not None

    client.files.append(PlaudFileDTO(id="post-upgrade", filename="Post-upgrade upload"))
    assert sync_file_list(client, settings) == (1, 0)
    with session_scope() as session:
        assert session.get(PlaudFile, "post-upgrade").status == FileStatus.discovered
