"""Schema-free manual-note API contract and state isolation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest
from sqlalchemy import text


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv(
        "LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'manual-notes.db'}"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)

    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    with TestClient(app) as test_client:
        yield test_client


def _add_recording(
    file_id: str,
    *,
    status=None,
    is_trash: bool = False,
    **values,
) -> None:
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            PlaudFile(
                id=file_id,
                filename=f"{file_id}.mp3",
                status=status or FileStatus.discovered,
                is_trash=is_trash,
                **values,
            )
        )


@pytest.mark.parametrize(
    "status",
    [
        "discovered",
        "metadata_only",
        "downloading",
        "downloaded",
        "processing",
        "partial",
        "error",
        "done",
    ],
)
def test_manual_note_allows_every_file_status_without_audio_or_transcript(client, status):
    from localplaud.db.models import FileStatus, PlaudFile, Transcript
    from localplaud.db.session import session_scope

    file_id = f"recording-{status}"
    _add_recording(file_id, status=FileStatus(status))

    response = client.post(
        f"/api/files/{file_id}/notes",
        json={"title": f" {status} note ", "content_md": " body "},
    )

    assert response.status_code == 201
    assert response.json()["title"] == f"{status} note"
    assert response.json()["content_md"] == " body "
    with session_scope() as session:
        recording = session.get(PlaudFile, file_id)
        assert recording is not None
        assert recording.audio_path is None
        assert recording.wav_path is None
        assert session.query(Transcript).filter_by(file_id=file_id).count() == 0


def test_manual_note_rejects_unknown_and_trashed_recordings(client):
    _add_recording("trashed", is_trash=True)

    unknown = client.post(
        "/api/files/missing/notes", json={"title": "Note", "content_md": "Body"}
    )
    trashed = client.post(
        "/api/files/trashed/notes", json={"title": "Note", "content_md": "Body"}
    )

    assert unknown.status_code == 404
    assert trashed.status_code == 409


def test_active_note_index_blocks_edit_and_delete_without_mutation(client):
    _add_recording("index-busy")
    created = client.post(
        "/api/files/index-busy/notes",
        json={"title": "Original", "content_md": "Original body"},
    )
    assert created.status_code == 201
    note_id = created.json()["id"]

    from localplaud.db.models import KnowledgeDocument, UserNote, UserNoteRevision
    from localplaud.db.session import session_scope

    with session_scope() as session:
        document = session.query(KnowledgeDocument).filter_by(user_note_id=note_id).one()
        document.status = "running"
        document.lease_token = "active-index"
        document.lease_until = datetime.now(UTC) + timedelta(minutes=5)

    edited = client.put(
        f"/api/notes/{note_id}",
        json={"title": "Changed", "content_md": "Changed body", "base_version": 1},
    )
    deleted = client.delete(f"/api/notes/{note_id}")
    assert edited.status_code == deleted.status_code == 409
    assert edited.json()["detail"] == deleted.json()["detail"]
    assert "currently indexing" in edited.json()["detail"]
    with session_scope() as session:
        note = session.get(UserNote, note_id)
        assert note.title == "Original"
        assert note.content_md == "Original body"
        assert note.version == 1
        assert session.query(UserNoteRevision).filter_by(note_id=note_id).count() == 0
        assert session.query(KnowledgeDocument).filter_by(user_note_id=note_id).count() == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"title": " \n\t ", "content_md": "Body"},
        {"title": "Note", "content_md": " \n\t "},
        {"title": "T" * 201, "content_md": "Body"},
        {"title": "Note", "content_md": "M" * 200_001},
    ],
)
def test_manual_note_rejects_empty_or_too_long_trimmed_values(client, payload):
    _add_recording("bounds")

    response = client.post("/api/files/bounds/notes", json=payload)

    assert response.status_code == 422


def test_manual_note_accepts_trimmed_values_at_exact_maximums(client):
    _add_recording("max-bounds")
    title = "T" * 200
    content = "\n    code\n" + ("M" * 199_990)

    response = client.post(
        "/api/files/max-bounds/notes",
        json={"title": f" {title}\n", "content_md": content},
    )

    assert response.status_code == 201
    assert response.json()["title"] == title
    assert response.json()["content_md"] == content


@pytest.mark.parametrize(
    "extra",
    [
        {"source_type": "ask"},
        {"ask_message_id": 7},
        {"source_summary_id": 8},
        {"source_summary_snapshot": {"model": "spoofed"}},
        {"citations": [{"file_id": "other"}]},
    ],
)
def test_manual_note_rejects_spoofed_provenance_fields(client, extra):
    _add_recording("strict-body")

    response = client.post(
        "/api/files/strict-body/notes",
        json={"title": "Note", "content_md": "Body"} | extra,
    )

    assert response.status_code == 422


def test_manual_note_persists_exact_content_and_forced_provenance(client):
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope

    _add_recording("provenance")

    content = "\n    indented code\n\n# Exact body\n\nText.\n"
    response = client.post(
        "/api/files/provenance/notes",
        json={"title": "  Manual title  ", "content_md": content},
    )

    assert response.status_code == 201
    expected = {
        "id": response.json()["id"],
        "file_id": "provenance",
        "title": "Manual title",
        "content_md": content,
        "source_type": "manual",
        "ask_message_id": None,
        "source_summary_id": None,
        "source_summary_snapshot": None,
        "citations": [],
        "version": 1,
    }
    assert response.json() == expected
    with session_scope() as session:
        note = session.get(UserNote, expected["id"])
        assert note is not None
        assert {
            "file_id": note.file_id,
            "title": note.title,
            "content_md": note.content_md,
            "source_type": note.source_type,
            "ask_message_id": note.ask_message_id,
            "source_summary_id": note.source_summary_id,
            "source_summary_snapshot": note.source_summary_snapshot,
            "citations": note.citations,
            "version": note.version,
        } == {key: value for key, value in expected.items() if key != "id"}


def test_manual_note_list_update_delete_continuity_and_shared_contract(client):
    _add_recording("crud")
    created = client.post(
        "/api/files/crud/notes", json={"title": "First", "content_md": "Initial"}
    )
    assert created.status_code == 201
    note_id = created.json()["id"]

    listed = client.get("/api/notes?file_id=crud")
    assert listed.status_code == 200
    assert [note["id"] for note in listed.json()["notes"]] == [note_id]

    title = "U" * 200
    content = "\n    updated code\n" + ("C" * 199_982)
    updated = client.put(
        f"/api/notes/{note_id}",
        json={"title": f" {title}\n", "content_md": content, "base_version": 1},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == title
    assert updated.json()["content_md"] == content

    for payload in (
        {"title": " ", "content_md": "Body", "base_version": 2},
        {"title": "U" * 201, "content_md": "Body", "base_version": 2},
        {"title": "Valid", "content_md": "C" * 200_001, "base_version": 2},
        {
            "title": "Valid",
            "content_md": "Body",
            "base_version": 2,
            "source_type": "manual",
        },
    ):
        assert client.put(f"/api/notes/{note_id}", json=payload).status_code == 422

    unchanged = client.get("/api/notes?file_id=crud").json()["notes"]
    assert unchanged[0]["title"] == title
    assert unchanged[0]["content_md"] == content
    assert client.delete(f"/api/notes/{note_id}").status_code == 204
    assert client.get("/api/notes?file_id=crud").json()["notes"] == []


def test_legacy_note_update_without_base_version_preserves_history(client):
    _add_recording("legacy-update")
    created = client.post(
        "/api/files/legacy-update/notes",
        json={"title": "Original", "content_md": "Original body"},
    ).json()

    updated = client.put(
        f"/api/notes/{created['id']}",
        json={"title": "Legacy client", "content_md": "Updated body"},
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    history = client.get(f"/api/notes/{created['id']}/history").json()["items"]
    assert history[0]["version"] == 1
    assert history[0]["title"] == "Original"


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_manual_note_creation_serializes_with_concurrent_trash_update(
    client, monkeypatch, journal_mode
):
    from fastapi import HTTPException

    import localplaud.api.notes as service
    from localplaud.db.models import PlaudFile, UserNote
    from localplaud.db.session import get_engine, session_scope

    engine = get_engine()
    with engine.connect() as connection:
        selected_mode = connection.exec_driver_sql(
            f"PRAGMA journal_mode={journal_mode}"
        ).scalar_one()
    assert selected_mode.lower() == journal_mode
    _add_recording("trash-race")

    trash_has_reservation = Event()
    release_trash = Event()
    create_attempted_reservation = Event()
    original_serialize = service._serialize_manual_note_creation

    def coordinated_serialize(session):
        create_attempted_reservation.set()
        original_serialize(session)

    def move_to_trash() -> None:
        with session_scope() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            recording = session.get(PlaudFile, "trash-race")
            assert recording is not None
            recording.is_trash = True
            session.flush()
            trash_has_reservation.set()
            assert release_trash.wait(3)

    monkeypatch.setattr(service, "_serialize_manual_note_creation", coordinated_serialize)
    body = service.NoteBody(title="Blocked", content_md="Must not be saved")
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="trash") as trash_pool,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="create-note") as create_pool,
    ):
        trash_future = trash_pool.submit(move_to_trash)
        assert trash_has_reservation.wait(3)
        create_future = create_pool.submit(service.create_manual_note, "trash-race", body)
        assert create_attempted_reservation.wait(3)
        assert not create_future.done()
        release_trash.set()
        trash_future.result(timeout=3)
        with pytest.raises(HTTPException) as exc_info:
            create_future.result(timeout=3)

    assert exc_info.value.status_code == 409
    with session_scope() as session:
        recording = session.get(PlaudFile, "trash-race")
        assert recording is not None and recording.is_trash is True
        assert session.query(UserNote).filter_by(file_id="trash-race").count() == 0


def _schema_snapshot(connection) -> list[tuple]:
    return list(
        connection.execute(
            text(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ).tuples()
    )


def _data_snapshot(connection) -> dict[str, list[tuple]]:
    tables = connection.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' "
            "AND name NOT IN ('user_notes', 'knowledge_documents', 'knowledge_chunks') "
            "ORDER BY name"
        )
    ).scalars()
    return {
        table: list(connection.execute(text(f'SELECT * FROM "{table}" ORDER BY rowid')).tuples())
        for table in tables
    }


def test_manual_note_does_not_change_schema_or_recording_pipeline_derived_state(client):
    from localplaud.db.models import (
        Chunk,
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Summary,
        Transcript,
    )
    from localplaud.db.session import get_engine, session_scope

    lease = datetime.now(UTC) + timedelta(minutes=10)
    with session_scope() as session:
        recording = PlaudFile(
            id="protected-state",
            filename="protected.mp3",
            status=FileStatus.processing,
            audio_path="/audio/original.opus",
            wav_path="/audio/derived.wav",
            error="existing error",
            pipeline_retry_count=3,
            pipeline_next_retry_at=lease,
            pipeline_last_failure_at=lease - timedelta(minutes=5),
            processing_token="claim-token",
            processing_lease_until=lease,
        )
        session.add(recording)
        session.flush()
        session.add_all(
            [
                Transcript(
                    file_id=recording.id,
                    provider="test-asr",
                    model="test-model",
                    source="local",
                    text="Canonical transcript",
                    segments=[{"start": 0, "end": 1, "text": "Canonical transcript"}],
                ),
                Summary(
                    file_id=recording.id,
                    template="meeting",
                    title="Generated summary",
                    content_md="Generated content",
                    llm_provider="test-llm",
                    model="test-model",
                    source="local",
                ),
                Chunk(
                    file_id=recording.id,
                    idx=0,
                    text="Indexed content",
                    embedding_model="test-embedding",
                    dim=1,
                    embedding=b"data",
                ),
                StageRun(
                    file_id=recording.id,
                    stage=StageName.transcribe,
                    status=StageStatus.running,
                    attempts=2,
                    provider="test-asr",
                    model="test-model",
                    detail={"preserve": True},
                ),
            ]
        )

    engine = get_engine()
    with engine.connect() as connection:
        schema_before = _schema_snapshot(connection)
        data_before = _data_snapshot(connection)

    response = client.post(
        "/api/files/protected-state/notes",
        json={"title": "Independent note", "content_md": "User-authored content"},
    )
    assert response.status_code == 201

    with engine.connect() as connection:
        assert _schema_snapshot(connection) == schema_before
        assert _data_snapshot(connection) == data_before
