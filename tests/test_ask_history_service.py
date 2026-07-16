"""Focused coverage for the schema-free Ask history service."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, current_thread

import pytest
from sqlalchemy import event, inspect, select, text


@pytest.fixture
def ask_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.models import Base, FileStatus, PlaudFile

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'ask.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    engine = db_session.get_engine()
    Base.metadata.create_all(engine)
    with db_session.session_scope() as session:
        session.add_all(
            [
                PlaudFile(id="r1", filename="Recording one", status=FileStatus.done),
                PlaudFile(id="r2", filename="Recording two", status=FileStatus.done),
            ]
        )
    yield engine
    engine.dispose()
    db_session._engine = None
    db_session._Session = None


def _add_thread(
    session,
    thread_id: str,
    file_id: str | None,
    title: str,
    *,
    messages: tuple[tuple[str, str], ...] = (),
    retrieval_scope: dict | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
):
    from localplaud.db.models import AskMessage, AskThread

    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    thread = AskThread(
        id=thread_id,
        file_id=file_id,
        title=title,
        retrieval_scope=retrieval_scope or {},
        created_at=created_at or now,
        updated_at=updated_at or now,
    )
    session.add(thread)
    session.flush()
    rows = [AskMessage(thread_id=thread_id, role=role, content=content) for role, content in messages]
    session.add_all(rows)
    session.flush()
    return thread, rows


def _schema(engine) -> dict[str, tuple[str, ...]]:
    inspector = inspect(engine)
    return {
        table: tuple(column["name"] for column in inspector.get_columns(table))
        for table in inspector.get_table_names()
    }


def test_list_and_get_are_isolated_to_the_exact_surface(ask_db):
    from localplaud.ask_threads import get_thread, list_threads
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _add_thread(session, "library", None, "Library")
        _add_thread(session, "recording-1", "r1", "R1")
        _add_thread(session, "recording-2", "r2", "R2")

    assert [row["thread_id"] for row in list_threads(None)["threads"]] == ["library"]
    assert [row["thread_id"] for row in list_threads("r1")["threads"]] == ["recording-1"]
    assert [row["thread_id"] for row in list_threads("r2")["threads"]] == ["recording-2"]
    assert list_threads("missing") == {
        "threads": [],
        "total": 0,
        "page": 1,
        "page_size": 20,
        "pages": 1,
    }
    assert get_thread("library", file_id=None)["thread_id"] == "library"
    assert get_thread("recording-1", file_id="r1")["thread_id"] == "recording-1"

    for thread_id, wrong_file_id in (
        ("library", "r1"),
        ("recording-1", None),
        ("recording-1", "r2"),
        ("recording-2", "r1"),
    ):
        with pytest.raises(LookupError, match="thread not found"):
            get_thread(thread_id, file_id=wrong_file_id)


def test_list_search_is_case_insensitive_literal_and_includes_messages(ask_db):
    from localplaud.ask_threads import list_threads
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _add_thread(session, "literal", "r1", r"Budget 100%_done\\path")
        _add_thread(session, "wildcard-lookalike", "r1", "Budget 100XXdone/path")
        _add_thread(
            session,
            "message-hit",
            "r1",
            "Unrelated title",
            messages=(("user", "Where is the MixedCase Needle?"),),
        )
        _add_thread(
            session,
            "wrong-surface-hit",
            "r2",
            "100%_done",
            messages=(("assistant", "mixedcase needle"),),
        )

    assert [
        row["thread_id"] for row in list_threads("r1", query="budget 100%_")["threads"]
    ] == ["literal"]
    assert [row["thread_id"] for row in list_threads("r1", query=r"%_done\\")["threads"]] == [
        "literal"
    ]
    assert [row["thread_id"] for row in list_threads("r1", query="mixedcase needle")["threads"]] == [
        "message-hit"
    ]


def test_list_paginates_deterministically_and_validates_bounds(ask_db):
    from localplaud.ask_threads import list_threads
    from localplaud.db.session import session_scope

    tied = datetime(2026, 7, 16, 8, tzinfo=UTC)
    with session_scope() as session:
        for index in range(47):
            _add_thread(
                session,
                f"thread-{index:02d}",
                "r1",
                f"Question {index}",
                updated_at=tied,
            )
        _add_thread(session, "library-extra", None, "Not in recording", updated_at=tied)
        _add_thread(session, "r2-extra", "r2", "Not in recording", updated_at=tied)

    first = list_threads("r1", page=1, page_size=20)
    second = list_threads("r1", page=2, page_size=20)
    third = list_threads("r1", page=3, page_size=20)
    expected = [f"thread-{index:02d}" for index in reversed(range(47))]
    assert {key: first[key] for key in ("total", "page", "page_size", "pages")} == {
        "total": 47,
        "page": 1,
        "page_size": 20,
        "pages": 3,
    }
    assert [row["thread_id"] for row in first["threads"]] == expected[:20]
    assert [row["thread_id"] for row in second["threads"]] == expected[20:40]
    assert [row["thread_id"] for row in third["threads"]] == expected[40:]
    clamped = list_threads("r1", page=99, page_size=20)
    assert clamped["page"] == 3
    assert [row["thread_id"] for row in clamped["threads"]] == expected[40:]

    for kwargs in (
        {"page": 0},
        {"page": True},
        {"page": 1.5},
        {"page_size": 0},
        {"page_size": 101},
        {"page_size": False},
    ):
        with pytest.raises(ValueError):
            list_threads("r1", **kwargs)


def test_list_metadata_counts_preview_and_uses_constant_queries(ask_db):
    from localplaud.ask_threads import list_threads
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope

    created = datetime(2026, 7, 15, 9, tzinfo=UTC)
    updated = created + timedelta(hours=2)
    with session_scope() as session:
        _, messages = _add_thread(
            session,
            "metadata",
            None,
            "Metadata thread",
            messages=(
                ("user", "First question"),
                ("assistant", "Grounded answer"),
                ("user", "L" * 220),
            ),
            retrieval_scope={"file_ids": ["r1", "r2"]},
            created_at=created,
            updated_at=updated,
        )
        _add_thread(session, "empty", None, "Empty thread", updated_at=created)
        session.add(
            UserNote(
                file_id=None,
                title="Saved answer",
                content_md="Grounded answer",
                source_type="ask",
                ask_message_id=messages[1].id,
                citations=[{"file_id": "r1", "start": 12.0}],
            )
        )

    statements = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(ask_db, "before_cursor_execute", record_statement)
    try:
        result = list_threads(None)
    finally:
        event.remove(ask_db, "before_cursor_execute", record_statement)

    assert len(statements) == 2
    metadata, empty = result["threads"]
    assert metadata == {
        "thread_id": "metadata",
        "title": "Metadata thread",
        "file_id": None,
        "retrieval_scope": {"file_ids": ["r1", "r2"]},
        "created_at": created.isoformat(),
        "updated_at": updated.isoformat(),
        "message_count": 3,
        "question_count": 2,
        "last_message_preview": "L" * 180,
        "saved_note_count": 1,
    }
    assert empty["message_count"] == 0
    assert empty["question_count"] == 0
    assert empty["last_message_preview"] is None
    assert empty["saved_note_count"] == 0


def test_rename_trims_validates_and_preserves_scope_and_messages(ask_db):
    from localplaud.ask_threads import rename_thread
    from localplaud.db.models import AskMessage, AskThread
    from localplaud.db.session import session_scope

    with session_scope() as session:
        thread, messages = _add_thread(
            session,
            "rename-me",
            "r1",
            "Original",
            messages=(("user", "Question"), ("assistant", "Answer")),
            retrieval_scope={"origin": "local"},
        )
        original = {
            "file_id": thread.file_id,
            "retrieval_scope": thread.retrieval_scope,
            "created_at": thread.created_at.replace(tzinfo=None),
            "message_ids": [message.id for message in messages],
        }
        original_updated_at = thread.updated_at.replace(tzinfo=None)

    for invalid in ("", "   ", "x" * 201, None):
        with pytest.raises(ValueError):
            rename_thread("rename-me", invalid, "r1")
    for wrong_file_id in (None, "r2"):
        with pytest.raises(LookupError, match="thread not found"):
            rename_thread("rename-me", "Wrong surface", wrong_file_id)

    renamed = rename_thread("rename-me", "  Renamed thread  ", "r1")
    assert renamed["title"] == "Renamed thread"
    with session_scope() as session:
        thread = session.get(AskThread, "rename-me")
        assert thread is not None
        assert thread.title == "Renamed thread"
        assert thread.file_id == original["file_id"]
        assert thread.retrieval_scope == original["retrieval_scope"]
        assert thread.created_at == original["created_at"]
        assert thread.updated_at != original_updated_at
        assert list(
            session.scalars(
                select(AskMessage.id)
                .where(AskMessage.thread_id == "rename-me")
                .order_by(AskMessage.id)
            )
        ) == original["message_ids"]


def test_delete_detaches_saved_notes_with_foreign_keys_off_and_changes_no_schema(ask_db):
    from localplaud.ask_threads import delete_thread, get_thread, note_to_dict
    from localplaud.db.models import AskMessage, AskThread, UserNote
    from localplaud.db.session import session_scope

    with ask_db.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 0

    with session_scope() as session:
        _, messages = _add_thread(
            session,
            "delete-me",
            "r1",
            "Delete me",
            messages=(("user", "What shipped?"), ("assistant", "The release shipped.")),
        )
        note = UserNote(
            file_id="r1",
            title="Release answer",
            content_md="# Release\n\nThe release shipped.",
            source_type="ask",
            ask_message_id=messages[1].id,
            citations=[
                {
                    "file_id": "r1",
                    "filename": "Recording one",
                    "start": 42.0,
                    "end": 45.0,
                    "speaker": "Sky",
                    "text": "shipped",
                }
            ],
        )
        session.add(note)
        session.flush()
        note_id = note.id
        before = note_to_dict(note)

    schema_before = _schema(ask_db)
    with pytest.raises(LookupError, match="thread not found"):
        delete_thread("delete-me", None)
    with pytest.raises(LookupError, match="thread not found"):
        delete_thread("delete-me", "r2")
    assert get_thread("delete-me", file_id="r1")["thread_id"] == "delete-me"

    result = delete_thread("delete-me", "r1")
    assert result == {
        "thread_id": "delete-me",
        "deleted_message_count": 2,
        "detached_saved_note_count": 1,
    }
    assert _schema(ask_db) == schema_before

    with session_scope() as session:
        assert session.get(AskThread, "delete-me") is None
        assert list(
            session.scalars(select(AskMessage).where(AskMessage.thread_id == "delete-me"))
        ) == []
        preserved = session.get(UserNote, note_id)
        assert preserved is not None
        after = note_to_dict(preserved)
        assert preserved.ask_message_id is None
        assert after == before | {"ask_message_id": None}
    with pytest.raises(LookupError, match="thread not found"):
        get_thread("delete-me", file_id="r1")


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_delete_serializes_with_concurrent_answer_promotion(
    ask_db, monkeypatch, journal_mode
):
    import localplaud.ask_threads as service
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope

    with ask_db.connect() as connection:
        selected_mode = connection.exec_driver_sql(
            f"PRAGMA journal_mode={journal_mode}"
        ).scalar_one()
    assert selected_mode.lower() == journal_mode

    with session_scope() as session:
        _, messages = _add_thread(
            session,
            "concurrent-delete",
            "r1",
            "Concurrent lifecycle",
            messages=(("user", "What shipped?"), ("assistant", "The beta shipped.")),
        )
        answer_id = messages[1].id

    original_serialize = service._serialize_saved_note_lifecycle
    save_has_reservation = Event()
    release_save = Event()
    delete_attempted_reservation = Event()

    def coordinated_serialize(session):
        is_save = current_thread().name.startswith("save-note")
        if not is_save:
            delete_attempted_reservation.set()
        original_serialize(session)
        if is_save:
            save_has_reservation.set()
            assert release_save.wait(3)

    monkeypatch.setattr(service, "_serialize_saved_note_lifecycle", coordinated_serialize)
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="save-note") as save_pool,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete-thread") as delete_pool,
    ):
        save_future = save_pool.submit(service.save_answer_as_note, answer_id)
        assert save_has_reservation.wait(3)
        delete_future = delete_pool.submit(
            service.delete_thread, "concurrent-delete", "r1"
        )
        assert delete_attempted_reservation.wait(3)
        assert not delete_future.done()
        release_save.set()
        saved = save_future.result(timeout=3)
        deleted = delete_future.result(timeout=3)

    assert saved["ask_message_id"] == answer_id
    assert deleted["detached_saved_note_count"] == 1
    with session_scope() as session:
        note = session.get(UserNote, saved["id"])
        assert note is not None
        assert note.ask_message_id is None
        assert note.content_md == "The beta shipped."
