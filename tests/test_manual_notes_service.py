"""Saved-note API for notes that already exist.

Creating blank manual notes was removed; notes created before that (and Ask
answers or editable copies) stay listable, editable, versioned and deletable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


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


def _add_note(file_id: str, title: str, content_md: str) -> int:
    """A manual note as created before blank-note creation was removed."""
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope
    from localplaud.worker.knowledge_index import sync_user_note_document

    with session_scope() as session:
        note = UserNote(
            file_id=file_id, title=title, content_md=content_md, source_type="manual", citations=[]
        )
        session.add(note)
        session.flush()
        sync_user_note_document(session, note)
        return note.id


def test_blank_note_creation_endpoint_is_removed(client):
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope

    _add_recording("no-create")
    response = client.post(
        "/api/files/no-create/notes", json={"title": "Note", "content_md": "Body"}
    )
    assert response.status_code in (404, 405)
    with session_scope() as session:
        assert session.query(UserNote).count() == 0


def test_active_note_index_blocks_edit_and_delete_without_mutation(client):
    _add_recording("index-busy")
    note_id = _add_note("index-busy", "Original", "Original body")

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


def test_manual_note_list_update_delete_continuity_and_shared_contract(client):
    _add_recording("crud")
    note_id = _add_note("crud", "First", "Initial")

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
    note_id = _add_note("legacy-update", "Original", "Original body")

    updated = client.put(
        f"/api/notes/{note_id}",
        json={"title": "Legacy client", "content_md": "Updated body"},
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2

    history = client.get(f"/api/notes/{note_id}/history").json()["items"]
    assert history[0]["version"] == 1
    assert history[0]["title"] == "Original"
