"""Versioned history for manual, Ask-saved, and editable-copy notes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import create_engine, event, inspect, select, text


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv(
        "LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'note-history.db'}"
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


def _add_recording(file_id: str = "history") -> None:
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id=file_id, filename=f"{file_id}.mp3"))


def _create_note(client, *, title: str = "Version one", content: str = "Original") -> dict:
    _add_recording()
    response = client.post(
        "/api/files/history/notes",
        json={"title": title, "content_md": content},
    )
    assert response.status_code == 201
    return response.json()


def test_edit_archives_current_noop_does_not_and_stale_write_conflicts(client):
    from localplaud.db.models import UserNoteRevision
    from localplaud.db.session import session_scope

    created = _create_note(client)
    assert created["version"] == 1

    changed = client.put(
        f"/api/notes/{created['id']}",
        json={"title": "Version two", "content_md": "Changed", "base_version": 1},
    )
    assert changed.status_code == 200
    assert changed.json()["version"] == 2

    noop = client.put(
        f"/api/notes/{created['id']}",
        json={"title": "Version two", "content_md": "Changed", "base_version": 2},
    )
    assert noop.status_code == 200
    assert noop.json()["version"] == 2

    stale = client.put(
        f"/api/notes/{created['id']}",
        json={"title": "Lost edit", "content_md": "Must not win", "base_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "note_changed",
        "message": "note changed; reload before saving",
        "current_version": 2,
    }
    with session_scope() as session:
        revisions = list(session.scalars(select(UserNoteRevision)))
        assert [(row.version, row.title, row.content_md) for row in revisions] == [
            (1, "Version one", "Original")
        ]
        assert revisions[0].content_preview == "Original"


def test_revision_preview_uses_the_safe_markdown_renderer(client):
    content = (
        "# Heading\n\n| Owner | Action |\n| --- | --- |\n| Sky | Ship |\n\n"
        "<script>alert('x')</script> [unsafe](javascript:alert(1))"
    )
    created = _create_note(client, content=content)
    assert client.put(
        f"/api/notes/{created['id']}",
        json={"title": "Changed", "content_md": "Current", "base_version": 1},
    ).status_code == 200

    preview = client.get(f"/api/notes/{created['id']}/history/1")
    assert preview.status_code == 200
    rendered = preview.json()["content_html"]
    assert "<h1>Heading</h1>" in rendered and "<table>" in rendered
    assert "&lt;script&gt;alert('x')&lt;/script&gt;" in rendered
    assert "<script>alert('x')</script>" not in rendered
    assert 'href="javascript:' not in rendered


def test_restore_creates_new_live_version_and_preserves_immutable_provenance(client):
    from localplaud.db.models import UserNote, UserNoteRevision
    from localplaud.db.session import session_scope

    _add_recording()
    immutable = {
        "source_type": "ask",
        "ask_message_id": None,
        "source_summary_id": None,
        "source_summary_snapshot": {"provider": "local", "revision": 7},
        "citations": [{"file_id": "history", "start": 12.5, "text": "Grounded"}],
    }
    with session_scope() as session:
        note = UserNote(
            file_id="history",
            title="Original title",
            content_md="Original body",
            **immutable,
        )
        session.add(note)
        session.flush()
        note_id = note.id

    edited = client.put(
        f"/api/notes/{note_id}",
        json={"title": "Current title", "content_md": "Current body", "base_version": 1},
    )
    assert edited.status_code == 200
    restored = client.post(
        f"/api/notes/{note_id}/history/1/restore",
        json={"base_version": 2},
    )
    assert restored.status_code == 200
    assert (restored.json()["title"], restored.json()["content_md"]) == (
        "Original title",
        "Original body",
    )
    assert restored.json()["version"] == 3
    for key, value in immutable.items():
        assert restored.json()[key] == value

    with session_scope() as session:
        live = session.get(UserNote, note_id)
        assert live is not None
        for key, value in immutable.items():
            assert getattr(live, key) == value
        revisions = list(
            session.scalars(
                select(UserNoteRevision)
                .where(UserNoteRevision.note_id == note_id)
                .order_by(UserNoteRevision.version)
            )
        )
        assert [(row.version, row.title, row.content_md) for row in revisions] == [
            (1, "Original title", "Original body"),
            (2, "Current title", "Current body"),
        ]

    stale_restore = client.post(
        f"/api/notes/{note_id}/history/1/restore",
        json={"base_version": 2},
    )
    assert stale_restore.status_code == 409
    assert stale_restore.json()["detail"] == {
        "code": "note_changed",
        "message": "note changed; reload before restoring",
        "current_version": 3,
    }


def test_history_is_keyset_paginated_and_list_never_selects_archived_bodies(client):
    from localplaud.db.models import UserNoteRevision
    from localplaud.db.session import get_engine, session_scope

    created = _create_note(client, content="L" * 200_000)
    with session_scope() as session:
        session.add_all(
            UserNoteRevision(
                note_id=created["id"],
                version=version,
                title=f"Archived {version}",
                content_md=str(version) + ("B" * (200_000 - len(str(version)))),
            )
            for version in range(1, 56)
        )

    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "user_note_revisions" in statement:
            statements.append(statement.lower())

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        first = client.get(f"/api/notes/{created['id']}/history", params={"limit": 20})
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert first.status_code == 200
    assert [item["version"] for item in first.json()["items"]] == list(range(55, 35, -1))
    assert all(len(item["content_preview"]) <= 240 for item in first.json()["items"])
    assert all(item["archived_at"].endswith("+00:00") for item in first.json()["items"])
    assert first.json()["next_before_version"] == 36
    assert statements
    assert all("content_md" not in statement for statement in statements)

    second = client.get(
        f"/api/notes/{created['id']}/history",
        params={"limit": 20, "before_version": first.json()["next_before_version"]},
    )
    assert [item["version"] for item in second.json()["items"]] == list(range(35, 15, -1))
    detail = client.get(f"/api/notes/{created['id']}/history/55")
    assert detail.status_code == 200
    assert detail.json()["archived_at"].endswith("+00:00")
    assert len(detail.json()["content_md"]) == 200_000
    assert client.get(f"/api/notes/{created['id']}/history?limit=51").status_code == 422


def test_deleting_note_cascades_history(client):
    from localplaud.db.models import UserNoteRevision
    from localplaud.db.session import session_scope

    created = _create_note(client)
    assert client.put(
        f"/api/notes/{created['id']}",
        json={"title": "Changed", "content_md": "Changed", "base_version": 1},
    ).status_code == 200
    assert client.delete(f"/api/notes/{created['id']}").status_code == 204
    with session_scope() as session:
        assert session.query(UserNoteRevision).filter_by(note_id=created["id"]).count() == 0


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_concurrent_edits_have_one_winner_and_one_conflict(
    client, monkeypatch, journal_mode
):
    import localplaud.api.notes as service
    from localplaud.db.session import get_engine

    created = _create_note(client)
    with get_engine().connect() as connection:
        selected = connection.exec_driver_sql(f"PRAGMA journal_mode={journal_mode}").scalar_one()
    assert selected.lower() == journal_mode

    first_has_lock = Event()
    release_first = Event()
    original_archive = service._archive_live_note

    def coordinated_archive(session, note):
        original_archive(session, note)
        if not first_has_lock.is_set():
            first_has_lock.set()
            assert release_first.wait(3)

    monkeypatch.setattr(service, "_archive_live_note", coordinated_archive)

    def edit(title: str):
        return client.put(
            f"/api/notes/{created['id']}",
            json={"title": title, "content_md": title, "base_version": 1},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(edit, "First")
        assert first_has_lock.wait(3)
        second_future = pool.submit(edit, "Second")
        assert not second_future.done()
        release_first.set()
        responses = [first_future.result(timeout=3), second_future.result(timeout=3)]

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert next(response for response in responses if response.status_code == 409).json() == {
        "detail": {
            "code": "note_changed",
            "message": "note changed; reload before saving",
            "current_version": 2,
        }
    }


def test_additive_history_migration_preserves_legacy_notes_and_is_idempotent(tmp_path):
    from localplaud.db.migrations import migrate_editable_note_revision_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE user_notes (id INTEGER PRIMARY KEY, title VARCHAR(200) "
                "NOT NULL, content_md TEXT NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO user_notes (id, title, content_md) VALUES (1, 'Keep', 'Body')")
        )

    assert migrate_editable_note_revision_schema(engine) == [
        "user_notes.version",
        "user_note_revisions",
    ]
    assert migrate_editable_note_revision_schema(engine) == []
    metadata = inspect(engine)
    assert "version" in {column["name"] for column in metadata.get_columns("user_notes")}
    assert "user_note_revisions" in metadata.get_table_names()
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT title, content_md, version FROM user_notes WHERE id = 1")
        ).one() == ("Keep", "Body", 1)


def test_history_migration_ddl_is_cross_dialect():
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateTable

    from localplaud.db.migrations import editable_note_history_migration_statements
    from localplaud.db.models import UserNoteRevision

    legacy = {"user_notes": {"id", "title", "content_md"}}
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        statements = dict(editable_note_history_migration_statements(legacy, dialect))
        assert statements["user_notes.version"].endswith(
            "version INTEGER NOT NULL DEFAULT 1"
        )
        table_ddl = str(CreateTable(UserNoteRevision.__table__).compile(dialect=dialect))
        assert "user_note_revisions" in table_ddl
        assert "ON DELETE CASCADE" in table_ddl.upper()
        assert "uq_user_note_revision_note_version" in table_ddl

    current = {"user_notes": legacy["user_notes"] | {"version"}}
    assert editable_note_history_migration_statements(current, postgresql.dialect()) == []
