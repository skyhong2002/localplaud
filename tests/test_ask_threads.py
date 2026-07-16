"""Durable grounded Ask follow-ups and saved-note workflow."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import create_engine, inspect, select, text


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'ask.db'}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed():
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(id="r1", filename="Weekly Sync", status=FileStatus.done),
                PlaudFile(id="r2", filename="Interview", status=FileStatus.done),
            ]
        )


def _thread_id(html: str) -> str:
    match = re.search(r'name="thread_id" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_ask_skill_provenance_migration_is_idempotent(tmp_path):
    from localplaud.db.migrations import migrate_ask_provenance_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-ask.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE ask_messages (id INTEGER PRIMARY KEY, "
                "thread_id VARCHAR(36), role VARCHAR(16), content TEXT, sources JSON)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE ask_threads (id VARCHAR(36) PRIMARY KEY, "
                "file_id VARCHAR(64), title VARCHAR(200))"
            )
        )
    first = migrate_ask_provenance_schema(engine)
    assert "ask_messages.skill_key" in first
    assert "ask_messages.skill_snapshot" in first
    assert "ask_threads.retrieval_scope" in first
    assert migrate_ask_provenance_schema(engine) == []
    columns = {item["name"] for item in inspect(engine).get_columns("ask_messages")}
    assert {"skill_key", "skill_snapshot"} <= columns
    inspected_thread_columns = inspect(engine).get_columns("ask_threads")
    thread_columns = {item["name"] for item in inspected_thread_columns}
    assert "retrieval_scope" in thread_columns
    retrieval_scope = next(
        item for item in inspected_thread_columns if item["name"] == "retrieval_scope"
    )
    assert retrieval_scope["nullable"] is False


def test_ask_request_claim_migration_is_additive_and_idempotent(tmp_path):
    from localplaud.db.migrations import migrate_ask_request_claim_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'ask-claim.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE ask_threads (id VARCHAR(36) PRIMARY KEY, "
                "file_id VARCHAR(64), title VARCHAR(200), retrieval_scope JSON, "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
    assert migrate_ask_request_claim_schema(engine) == [
        "ask_threads.request_token",
        "ask_threads.request_lease_until",
        "ask_threads.request_owner",
    ]
    assert migrate_ask_request_claim_schema(engine) == []
    columns = {item["name"] for item in inspect(engine).get_columns("ask_threads")}
    assert {"request_token", "request_lease_until", "request_owner"} <= columns


def test_legacy_deployed_ask_schema_is_rebuilt_without_losing_messages(tmp_path):
    from localplaud.db.migrations import migrate_ask_provenance_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-deployed-ask.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO plaud_files (id) VALUES ('recording')"))
        connection.execute(
            text("""
            CREATE TABLE ask_threads (
                id INTEGER PRIMARY KEY, file_id VARCHAR(64), title VARCHAR(256),
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            )
        """)
        )
        connection.execute(
            text("""
            CREATE TABLE ask_messages (
                id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL,
                role VARCHAR(16) NOT NULL, content TEXT NOT NULL,
                citations JSON NOT NULL, provider VARCHAR(64), model VARCHAR(128),
                profile_snapshot JSON NOT NULL, usage JSON NOT NULL,
                estimated_cost FLOAT, actual_cost FLOAT, created_at DATETIME NOT NULL
            )
        """)
        )
        connection.execute(
            text("""
            INSERT INTO ask_threads
                (id, file_id, title, created_at, updated_at)
            VALUES (7, 'recording', 'History', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """)
        )
        connection.execute(
            text("""
            INSERT INTO ask_messages (
                id, thread_id, role, content, citations, provider, model,
                profile_snapshot, usage, estimated_cost, created_at
            ) VALUES (
                9, 7, 'assistant', 'Answer', '[{"second": 12}]', 'ollama', 'qwen',
                '{"version": 2}', '{"output_tokens": 4}', 0.5, CURRENT_TIMESTAMP
            )
        """)
        )

    assert migrate_ask_provenance_schema(engine) == ["ask_threads", "ask_messages"]
    assert migrate_ask_provenance_schema(engine) == []
    thread_columns = {item["name"] for item in inspect(engine).get_columns("ask_threads")}
    message_columns = {item["name"] for item in inspect(engine).get_columns("ask_messages")}
    assert "retrieval_scope" in thread_columns
    assert {"sources", "resolved_profile_snapshot", "estimated_cost_usd"} <= message_columns
    assert not {"citations", "profile_snapshot", "actual_cost"} & message_columns
    with engine.connect() as connection:
        thread = connection.execute(
            text("SELECT id, file_id, title, retrieval_scope FROM ask_threads")
        ).one()
        message = connection.execute(
            text("""
            SELECT id, thread_id, sources, resolved_profile_snapshot, usage,
                   estimated_cost_usd
            FROM ask_messages
        """)
        ).one()
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    assert tuple(thread) == ("7", "recording", "History", "{}")
    assert message.id == 9 and message.thread_id == "7"
    assert '"second": 12' in message.sources
    assert '"version": 2' in message.resolved_profile_snapshot
    assert '"output_tokens": 4' in message.usage
    assert message.estimated_cost_usd == 0.5


def test_editable_note_source_migration_is_idempotent(tmp_path):
    from localplaud.db.migrations import migrate_editable_note_source_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-notes.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE user_notes (id INTEGER PRIMARY KEY)"))
    assert migrate_editable_note_source_schema(engine) == ["user_notes.source_summary_id"]
    assert migrate_editable_note_source_schema(engine) == []
    columns = {item["name"] for item in inspect(engine).get_columns("user_notes")}
    assert "source_summary_id" in columns


def test_ask_history_api_and_accessible_drawer_contract(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import AskMessage, AskThread, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        library = AskThread(id="library-history", file_id=None, title="Library decisions")
        recording = AskThread(id="recording-history", file_id="r1", title="Weekly follow-up")
        session.add_all([library, recording])
        session.flush()
        session.add_all(
            [
                AskMessage(thread_id=library.id, role="user", content="Compare recordings"),
                AskMessage(thread_id=library.id, role="assistant", content="Library answer"),
                AskMessage(thread_id=recording.id, role="user", content="What shipped?"),
                AskMessage(thread_id=recording.id, role="assistant", content="The beta shipped"),
            ]
        )
        session.flush()
        answer = session.scalar(
            select(AskMessage).where(
                AskMessage.thread_id == recording.id,
                AskMessage.role == "assistant",
            )
        )
        session.add(
            UserNote(
                file_id="r1",
                title="Saved release answer",
                content_md="The beta shipped",
                source_type="ask",
                ask_message_id=answer.id,
                citations=[{"file_id": "r1", "start": 12.0}],
            )
        )

    library_history = client.get("/api/ask/threads").json()
    recording_history = client.get("/api/ask/threads?file_id=r1").json()
    assert [item["thread_id"] for item in library_history["threads"]] == ["library-history"]
    assert [item["thread_id"] for item in recording_history["threads"]] == ["recording-history"]
    assert recording_history["threads"][0]["saved_note_count"] == 1
    assert (
        client.patch(
            "/api/ask/threads/recording-history", json={"title": "Wrong surface"}
        ).status_code
        == 404
    )
    renamed = client.patch(
        "/api/ask/threads/recording-history?file_id=r1",
        json={"title": "  Release follow-up  "},
    )
    assert renamed.status_code == 200 and renamed.json()["title"] == "Release follow-up"
    trimmed_limit = client.patch(
        "/api/ask/threads/recording-history?file_id=r1",
        json={"title": f"  {'x' * 200}  "},
    )
    assert trimmed_limit.status_code == 200 and len(trimmed_limit.json()["title"]) == 200
    assert (
        client.patch(
            "/api/ask/threads/recording-history?file_id=r1", json={"title": "   "}
        ).status_code
        == 422
    )

    library_page = client.get("/?ask=true&ask_thread=library-history")
    detail_page = client.get("/file/r1?tab=ask&ask_thread=recording-history")
    for page in (library_page, detail_page):
        assert "data-open-ask-history" in page.text
        assert 'id="ask-history-backdrop" hidden' in page.text
        assert 'role="dialog" aria-modal="true" aria-labelledby="ask-history-title"' in page.text
        assert "region.inert=true" in page.text
        assert "event.key==='Escape'" in page.text
        assert "document.activeElement===last" in page.text
        assert "if(restoreFocus)opener?.focus()" in page.text
        assert "signal:cleanupController.signal" in page.text
        assert "-webkit-line-clamp:2" in page.text
        assert "if(!backdrop.hidden)return" in page.text
        assert "candidate.dataset.threadId===item.thread_id" in page.text
    assert 'const fileId=null,selectedId="library-history"' in library_page.text
    assert 'const fileId="r1",selectedId="recording-history"' in detail_page.text

    deleted = client.delete("/api/ask/threads/recording-history?file_id=r1")
    assert deleted.status_code == 200
    assert deleted.json()["detached_saved_note_count"] == 1
    with session_scope() as session:
        note = session.scalar(select(UserNote).where(UserNote.title == "Saved release answer"))
        assert note is not None and note.ask_message_id is None
        assert note.content_md == "The beta shipped"


def test_single_recording_followup_persists_history_and_sources(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    histories = []

    def fake_answer(query, **kwargs):
        histories.append(kwargs.get("history"))
        return {
            "answer": f"Grounded answer to {query}",
            "sources": [
                {
                    "score": 0.9,
                    "text": "we shipped the beta",
                    "start": 42.0,
                    "end": 45.0,
                    "speaker": "SPEAKER_00",
                    "file_id": "r1",
                    "filename": "Weekly Sync",
                }
            ],
        }

    monkeypatch.setattr("localplaud.worker.qa.answer", fake_answer)
    first = client.post("/file/r1/ask", data={"q": "What shipped?"})
    assert first.status_code == 200
    thread_id = _thread_id(first.text)
    assert "Save as note" in first.text and 'data-seek="42.0"' in first.text
    second = client.post("/file/r1/ask", data={"q": "Who confirmed it?", "thread_id": thread_id})
    assert second.status_code == 200
    assert second.text.count("Grounded answer") == 2
    assert histories[0] == []
    assert [item["role"] for item in histories[1]] == ["user", "assistant"]
    reopened = client.get(f"/file/r1?ask_thread={thread_id}")
    assert reopened.status_code == 200
    assert "Grounded answer to What shipped?" in reopened.text
    assert "Grounded answer to Who confirmed it?" in reopened.text
    assert "Recent threads" in reopened.text

    from localplaud.db.models import AskThread
    from localplaud.db.session import session_scope

    with session_scope() as session:
        thread = session.get(AskThread, thread_id)
        assert thread.file_id == "r1"
        assert [message.role for message in thread.messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
    assert client.post("/ask", data={"q": "wrong scope", "thread_id": thread_id}).status_code == 409


def test_persisted_ask_message_atomically_releases_cost_reservation(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import ProviderCostReservation
    from localplaud.db.session import session_scope

    def fake_answer(_query, **_kwargs):
        with session_scope() as session:
            session.add(
                ProviderCostReservation(
                    id="request-1:embed",
                    scope_key="file:r1",
                    file_id="r1",
                    operation="embed",
                    status="completed",
                    estimated_cost_usd=0.01,
                )
            )
        return {
            "answer": "Grounded",
            "sources": [],
            "estimated_cost_usd": 0.01,
            "_cost_reservation_ids": ["request-1:embed"],
        }

    monkeypatch.setattr("localplaud.worker.qa.answer", fake_answer)
    assert client.post("/file/r1/ask", data={"q": "What changed?"}).status_code == 200
    with session_scope() as session:
        assert session.get(ProviderCostReservation, "request-1:embed") is None


def test_follow_up_has_one_durable_provider_claim(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.ask_threads import ask_in_thread, delete_thread
    from localplaud.db.models import AskThread
    from localplaud.db.session import session_scope

    monkeypatch.setattr(
        "localplaud.worker.qa.answer",
        lambda *_args, **_kwargs: {"answer": "Initial", "sources": []},
    )
    initial = ask_in_thread("Initial question", file_id="r1")
    thread_id = initial["thread_id"]
    started = Event()
    release = Event()
    provider_calls: list[str] = []

    def blocking_answer(query, **_kwargs):
        provider_calls.append(query)
        started.set()
        assert release.wait(3)
        return {"answer": "Follow-up", "sources": []}

    monkeypatch.setattr("localplaud.worker.qa.answer", blocking_answer)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            ask_in_thread,
            "First follow-up",
            file_id="r1",
            thread_id=thread_id,
        )
        assert started.wait(3)
        with pytest.raises(ValueError, match="already answering"):
            ask_in_thread("Concurrent follow-up", file_id="r1", thread_id=thread_id)
        with pytest.raises(ValueError, match="currently answering"):
            delete_thread(thread_id, "r1")
        release.set()
        assert first.result(timeout=3)["messages"][-1]["content"] == "Follow-up"

    assert provider_calls == ["First follow-up"]
    with session_scope() as session:
        thread = session.get(AskThread, thread_id)
        assert thread.request_token is None and thread.request_lease_until is None
        assert len(thread.messages) == 4


def test_active_ask_rejects_no_egress_mutation_before_it_can_commit(
    monkeypatch, tmp_path
):
    _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.ask_threads import ask_in_thread
    from localplaud.db.session import session_scope
    from localplaud.providers.service import (
        ProfileMutationBusyError,
        lock_recording_profile_change,
    )

    started = Event()
    release = Event()
    provider_calls = 0

    def blocking_provider(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        started.set()
        assert release.wait(3)
        return {"answer": "Grounded", "sources": []}

    monkeypatch.setattr("localplaud.worker.qa.answer", blocking_provider)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(ask_in_thread, "Race", file_id="r1")
        assert started.wait(3)
        with session_scope() as session:
            with pytest.raises(ProfileMutationBusyError, match="Ask"):
                lock_recording_profile_change(session, "r1")
        release.set()
        assert request.result(timeout=3)["messages"][-1]["content"] == "Grounded"
    assert provider_calls == 1


def test_follow_up_claim_records_current_daemon_owner(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    import localplaud.poller.poll as poll_module
    from localplaud.ask_threads import ask_in_thread
    from localplaud.db.models import AskThread
    from localplaud.db.session import session_scope

    monkeypatch.setattr(
        "localplaud.worker.qa.answer",
        lambda *_args, **_kwargs: {"answer": "Initial", "sources": []},
    )
    thread_id = ask_in_thread("Initial", file_id="r1")["thread_id"]
    observed: list[str | None] = []

    def inspect_owner(*_args, **_kwargs):
        with session_scope() as session:
            observed.append(session.get(AskThread, thread_id).request_owner)
        return {"answer": "Owned", "sources": []}

    monkeypatch.setattr(poll_module, "_ACTIVE_DAEMON_OWNER", "daemon-owner")
    monkeypatch.setattr("localplaud.worker.qa.answer", inspect_owner)
    ask_in_thread("Follow-up", file_id="r1", thread_id=thread_id)
    assert observed == ["daemon-owner"]
    with session_scope() as session:
        thread = session.get(AskThread, thread_id)
        assert thread.request_owner is None


def test_startup_recovers_only_previous_daemon_ask_claims(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    from datetime import UTC, datetime, timedelta

    from localplaud.ask_threads import recover_ask_request_claims
    from localplaud.db.models import AskThread
    from localplaud.db.session import session_scope

    lease = datetime.now(UTC) + timedelta(hours=1)
    with session_scope() as session:
        session.add_all(
            [
                AskThread(
                    id="old",
                    title="Old",
                    request_token="old-token",
                    request_lease_until=lease,
                    request_owner="old-owner",
                ),
                AskThread(
                    id="live",
                    title="Live",
                    request_token="live-token",
                    request_lease_until=lease,
                    request_owner="live-owner",
                ),
            ]
        )
    assert recover_ask_request_claims("old-owner") == 1
    with session_scope() as session:
        old = session.get(AskThread, "old")
        live = session.get(AskThread, "live")
        assert old.request_token is None and old.request_owner is None
        assert live.request_token == "live-token" and live.request_owner == "live-owner"


def test_delete_thread_query_locks_postgresql_row_before_lease_check(monkeypatch, tmp_path):
    from sqlalchemy.dialects import postgresql

    import localplaud.ask_threads as ask_threads
    from localplaud.db.models import AskThread
    from localplaud.db.session import session_scope

    _client(monkeypatch, tmp_path)
    _seed()
    with session_scope() as session:
        session.add(AskThread(id="delete-me", file_id="r1", title="Delete me"))

    calls: list[bool] = []
    real_thread_for_surface = ask_threads._thread_for_surface

    def observe_lock(session, thread_id, file_id, *, for_update=False):
        calls.append(for_update)
        return real_thread_for_surface(session, thread_id, file_id, for_update=for_update)

    monkeypatch.setattr(ask_threads, "_thread_for_surface", observe_lock)
    ask_threads.delete_thread("delete-me", "r1")
    assert calls == [True]

    compiled = str(
        ask_threads._thread_query("thread", "r1", for_update=True).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "FOR UPDATE" in compiled
    assert "ask_threads.id = 'thread'" in compiled
    assert "ask_threads.file_id = 'r1'" in compiled


def test_failed_follow_up_releases_request_claim(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.ask_threads import ask_in_thread
    from localplaud.db.models import AskThread
    from localplaud.db.session import session_scope

    monkeypatch.setattr(
        "localplaud.worker.qa.answer",
        lambda *_args, **_kwargs: {"answer": "Initial", "sources": []},
    )
    thread_id = ask_in_thread("Initial", file_id="r1")["thread_id"]

    def fail(*_args, **_kwargs):
        raise RuntimeError("provider failed")

    monkeypatch.setattr("localplaud.worker.qa.answer", fail)
    with pytest.raises(RuntimeError, match="provider failed"):
        ask_in_thread("Follow-up", file_id="r1", thread_id=thread_id)
    with session_scope() as session:
        thread = session.get(AskThread, thread_id)
        assert thread.request_token is None and thread.request_lease_until is None


def test_failed_temporary_thread_cleanup_is_owned_and_atomic(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    from localplaud.ask_threads import _release_thread_request
    from localplaud.db.models import AskMessage, AskThread
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add_all(
            [
                AskThread(id="temporary", title="Temporary", request_token="new-owner"),
                AskThread(id="nonempty", title="Nonempty", request_token="owner"),
            ]
        )
        session.add(AskMessage(thread_id="nonempty", role="user", content="kept"))

    # A stale finally block must not release or delete a thread claimed by a
    # newer request between provider failure and cleanup.
    _release_thread_request("temporary", "stale-owner", delete_if_empty=True)
    with session_scope() as session:
        assert session.get(AskThread, "temporary").request_token == "new-owner"

    _release_thread_request("temporary", "new-owner", delete_if_empty=True)
    _release_thread_request("nonempty", "owner", delete_if_empty=True)
    with session_scope() as session:
        assert session.get(AskThread, "temporary") is None
        nonempty = session.get(AskThread, "nonempty")
        assert nonempty is not None
        assert nonempty.request_token is None
        assert [message.content for message in nonempty.messages] == ["kept"]


def test_follow_up_cannot_save_after_request_lease_expires(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    from datetime import UTC, datetime, timedelta

    from localplaud.ask_threads import ask_in_thread
    from localplaud.db.models import AskThread
    from localplaud.db.session import session_scope

    monkeypatch.setattr(
        "localplaud.worker.qa.answer",
        lambda *_args, **_kwargs: {"answer": "Initial", "sources": []},
    )
    thread_id = ask_in_thread("Initial", file_id="r1")["thread_id"]

    def expire_before_return(*_args, **_kwargs):
        with session_scope() as session:
            thread = session.get(AskThread, thread_id)
            thread.request_lease_until = datetime.now(UTC) - timedelta(seconds=1)
        return {"answer": "Must not persist", "sources": []}

    monkeypatch.setattr("localplaud.worker.qa.answer", expire_before_return)
    with pytest.raises(ValueError, match="lease changed"):
        ask_in_thread("Late answer", file_id="r1", thread_id=thread_id)
    with session_scope() as session:
        thread = session.get(AskThread, thread_id)
        assert [message.content for message in thread.messages] == ["Initial", "Initial"]
        assert thread.request_token is None and thread.request_lease_until is None


def test_lost_request_claim_stops_the_next_provider_dispatch(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    from datetime import UTC, datetime, timedelta

    import localplaud.worker.qa as qa_module
    from localplaud.ask_threads import ask_in_thread
    from localplaud.db.models import AskThread
    from localplaud.db.session import session_scope

    monkeypatch.setattr(
        "localplaud.worker.qa.answer",
        lambda *_args, **_kwargs: {"answer": "Initial", "sources": []},
    )
    thread_id = ask_in_thread("Initial", file_id="r1")["thread_id"]
    provider_calls: list[str] = []

    def simulated_answer(*_args, **_kwargs):
        qa_module._before_provider_dispatch()
        provider_calls.append("embed")
        with session_scope() as session:
            thread = session.get(AskThread, thread_id)
            thread.request_token = "takeover"
            thread.request_lease_until = datetime.now(UTC) + timedelta(hours=1)
        qa_module._before_provider_dispatch()
        provider_calls.append("llm")
        return {"answer": "Must not save", "sources": []}

    monkeypatch.setattr("localplaud.worker.qa.answer", simulated_answer)
    with pytest.raises(ValueError, match="lease changed before provider dispatch"):
        ask_in_thread("Follow-up", file_id="r1", thread_id=thread_id)
    assert provider_calls == ["embed"]

    with session_scope() as session:
        thread = session.get(AskThread, thread_id)
        assert thread.request_token == "takeover"
        assert [message.content for message in thread.messages] == ["Initial", "Initial"]


def test_ask_answers_render_safe_markdown(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    monkeypatch.setattr(
        "localplaud.worker.qa.answer",
        lambda query, **kwargs: {
            "answer": (
                "## Decision\n\n- Ship\n  - Friday\n\n"
                "| Owner | Task |\n| --- | --- |\n| Alex | Review |\n\n"
                "<script>alert('x')</script> [bad](javascript:alert(1))"
            ),
            "sources": [],
        },
    )

    long_query = "unbroken" * 20
    response = client.post("/file/r1/ask", data={"q": long_query})
    assert response.status_code == 200
    assert 'class="ask-user-message"' in response.text and long_query in response.text
    assert "<h2>Decision</h2>" in response.text
    assert "<table>" in response.text
    assert "&lt;script&gt;alert('x')&lt;/script&gt;" in response.text
    assert "<script>alert('x')</script>" not in response.text
    assert 'href="javascript:' not in response.text


def test_save_answer_is_idempotent_editable_and_visible(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()

    monkeypatch.setattr(
        "localplaud.worker.qa.answer",
        lambda query, **kwargs: {
            "answer": "The team decided to ship.",
            "sources": [
                {
                    "file_id": "r1",
                    "filename": "Weekly Sync",
                    "start": 12.0,
                    "end": 15.0,
                    "speaker": None,
                    "text": "ship it",
                    "score": 0.8,
                }
            ],
        },
    )
    response = client.post("/file/r1/ask", data={"q": "What was decided?"})
    message_id = int(re.search(r"saveAskNote\((\d+)", response.text).group(1))
    first = client.post(f"/api/ask/messages/{message_id}/save-note", json={})
    second = client.post(f"/api/ask/messages/{message_id}/save-note", json={})
    assert first.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    note_id = first.json()["id"]
    assert first.json()["file_id"] == "r1"
    assert first.json()["citations"][0]["start"] == 12.0
    from localplaud.db.models import KnowledgeDocument
    from localplaud.db.session import session_scope

    with session_scope() as session:
        documents = list(session.query(KnowledgeDocument))
        assert len(documents) == 1
        assert documents[0].user_note_id == note_id
        assert documents[0].status == "pending"

    notes_page = client.get("/notes")
    assert "The team decided to ship." in notes_page.text
    assert "Weekly Sync" in notes_page.text
    detail = client.get("/file/r1")
    assert "What was decided?" in detail.text
    assert f'data-note-panel="saved-{note_id}"' in detail.text

    long_title = "L" * 200
    assert (
        client.put(
            f"/api/notes/{note_id}",
            json={"title": long_title, "content_md": "Still grounded.", "base_version": 1},
        ).status_code
        == 200
    )
    long_notes_page = client.get("/notes")
    assert long_title in long_notes_page.text
    assert 'class="saved-note-head"' in long_notes_page.text
    assert 'class="saved-note-title"' in long_notes_page.text
    assert 'class="saved-note-actions"' in long_notes_page.text

    changed = client.put(
        f"/api/notes/{note_id}",
        json={
            "title": "Launch decision",
            "content_md": "Edited grounded note.",
            "base_version": 2,
        },
    )
    assert changed.status_code == 200
    assert client.get("/api/notes?file_id=r1").json()["notes"][0]["title"] == "Launch decision"
    exported = client.get(f"/api/notes/{note_id}/export.md")
    assert exported.status_code == 200
    assert "# Launch decision" in exported.text
    assert "- Weekly Sync @ 00:12" in exported.text
    assert client.delete(f"/api/notes/{note_id}").status_code == 204
    assert client.get("/api/notes").json()["notes"] == []


def test_oversized_ask_answer_cannot_create_an_uneditable_note(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import AskMessage, AskThread, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        thread = AskThread(id="oversized", file_id="r1", title="Oversized")
        session.add(thread)
        session.flush()
        message = AskMessage(
            thread_id=thread.id,
            role="assistant",
            content="X" * 200_001,
        )
        session.add(message)
        session.flush()
        message_id = message.id

    response = client.post(f"/api/ask/messages/{message_id}/save-note", json={})
    assert response.status_code == 409
    assert response.json()["detail"] == "content is too large to create an editable note"
    with session_scope() as session:
        assert session.query(UserNote).count() == 0


def test_oversized_generated_summary_cannot_create_an_uneditable_copy(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import Summary, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        summary = Summary(
            file_id="r1",
            template="oversized",
            content_md="X" * 200_001,
            source="local",
        )
        session.add(summary)
        session.flush()
        summary_id = summary.id

    response = client.post(f"/api/files/r1/summaries/{summary_id}/editable-copy")
    assert response.status_code == 409
    assert response.json()["detail"] == "content is too large to create an editable note"
    with session_scope() as session:
        assert session.query(UserNote).count() == 0


def test_blank_ask_and_generated_sources_cannot_create_uneditable_notes(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import AskMessage, AskThread, Summary, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        thread = AskThread(id="blank", file_id="r1", title="Blank")
        session.add(thread)
        session.flush()
        message = AskMessage(thread_id=thread.id, role="assistant", content=" \n ")
        summary = Summary(file_id="r1", template="blank", content_md="", source="local")
        session.add_all([message, summary])
        session.flush()
        message_id, summary_id = message.id, summary.id

    saved = client.post(f"/api/ask/messages/{message_id}/save-note", json={})
    copied = client.post(f"/api/files/r1/summaries/{summary_id}/editable-copy")
    assert saved.status_code == copied.status_code == 409
    assert saved.json()["detail"] == copied.json()["detail"] == "content must not be blank"
    with session_scope() as session:
        assert session.query(UserNote).count() == 0


def test_generated_summary_becomes_editable_copy_without_mutating_source(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import Summary
    from localplaud.db.session import session_scope

    with session_scope() as session:
        summary = Summary(
            file_id="r1",
            template="meeting",
            title="Weekly notes",
            content_md="# Generated\n\nOriginal AI output.",
            source="local",
        )
        session.add(summary)
        session.flush()
        summary_id = summary.id

    first = client.post(f"/api/files/r1/summaries/{summary_id}/editable-copy")
    second = client.post(f"/api/files/r1/summaries/{summary_id}/editable-copy")
    assert first.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    note_id = first.json()["id"]
    assert first.json()["source_type"] == "generated_summary"
    assert first.json()["source_summary_id"] == summary_id

    changed = client.put(
        f"/api/notes/{note_id}",
        json={
            "title": "Edited notes",
            "content_md": "User-owned correction.",
            "base_version": 1,
        },
    )
    assert changed.status_code == 200
    with session_scope() as session:
        assert session.get(Summary, summary_id).content_md == "# Generated\n\nOriginal AI output."

    detail = client.get(f"/file/r1?note_id={note_id}")
    assert 'data-summary-copy="' in detail.text
    assert f'data-workspace-note-form="{note_id}"' in detail.text
    assert f"const selectedNoteId={note_id}" in detail.text


def test_library_answer_with_multiple_recordings_saves_as_library_note(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    monkeypatch.setattr(
        "localplaud.worker.qa.answer",
        lambda query, **kwargs: {
            "answer": "Combined answer.",
            "sources": [
                {"file_id": "r1", "filename": "Weekly Sync", "start": 1, "text": "A"},
                {"file_id": "r2", "filename": "Interview", "start": 2, "text": "B"},
            ],
        },
    )
    response = client.post("/ask", data={"q": "Compare them"})
    thread_id = _thread_id(response.text)
    reopened = client.get(f"/?ask_thread={thread_id}")
    assert "Combined answer." in reopened.text and "Recent Ask" in reopened.text
    message_id = int(re.search(r"saveAskNote\((\d+)", response.text).group(1))
    note = client.post(f"/api/ask/messages/{message_id}/save-note", json={}).json()
    assert note["file_id"] is None
    assert len(note["citations"]) == 2
    assert "Library · Saved from Ask" in client.get("/notes").text


def test_grounded_quick_action_is_durable_versioned_and_non_mutating(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    calls = []

    def fake_answer(query, **kwargs):
        calls.append((query, kwargs))
        return {
            "answer": "| Task | Owner | Due | Status | Evidence |\n|---|---|---|---|---|",
            "sources": [
                {
                    "file_id": "r1",
                    "filename": "Weekly Sync",
                    "start": 8.0,
                    "end": 12.0,
                    "text": "Sky will prepare the draft",
                }
            ],
        }

    monkeypatch.setattr("localplaud.worker.qa.answer", fake_answer)
    catalog = client.get("/api/ask/skills")
    assert catalog.status_code == 200
    assert [item["key"] for item in catalog.json()["skills"]] == [
        "action_items",
        "task_table",
        "insights",
    ]
    response = client.post("/file/r1/ask/skill", data={"skill_key": "task_table"})
    assert response.status_code == 200
    assert "Task table" in response.text
    assert "quick action · v1" in response.text
    assert 'data-seek="8.0"' in response.text
    assert calls[0][0] == "tasks assignments owners deadlines deliverables follow up"
    assert "Create a Markdown table" in calls[0][1]["instruction"]
    assert calls[0][1]["file_id"] == "r1"

    from localplaud.db.models import AskMessage, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        messages = list(session.scalars(select(AskMessage).order_by(AskMessage.id)))
        assert {message.skill_key for message in messages} == {"task_table"}
        assert messages[0].skill_snapshot["version"] == 1
        assert messages[0].content == "Task table"
        assert list(session.scalars(select(UserNote))) == []

    assert client.post("/file/r1/ask/skill", data={"skill_key": "missing"}).status_code == 404
    assert (
        client.post("/file/missing/ask/skill", data={"skill_key": "task_table"}).status_code == 404
    )


def test_library_quick_action_is_grounded_durable_and_non_mutating(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    calls = []

    def fake_answer(query, **kwargs):
        calls.append((query, kwargs))
        return {
            "answer": "Cross-recording task table.",
            "sources": [
                {
                    "file_id": "r1",
                    "filename": "Weekly Sync",
                    "start": 8.0,
                    "text": "Sky will prepare the draft",
                },
                {
                    "file_id": "r2",
                    "filename": "Interview",
                    "start": 4.0,
                    "text": "Alex will review it",
                },
            ],
        }

    monkeypatch.setattr("localplaud.worker.qa.answer", fake_answer)
    page = client.get("/?ask=true")
    assert 'hx-post="/ask/skill"' in page.text
    assert 'data-ask-request data-ask-status="library-ask-status"' in page.text
    assert 'hx-sync="#answer:drop"' in page.text
    assert page.text.count('hx-sync="#answer:drop"') >= 2
    assert (
        'id="library-ask-status" class="ask-request-status" role="status" aria-live="polite"'
        in page.text
    )
    assert 'id="answer" role="region" aria-label="Answer"' in page.text
    assert "askErrorMessage" in page.text
    assert "if(question?.isConnected)question.value=''" in page.text
    assert "text.length<=300&&!text.includes('<')" in page.text
    assert "What decisions were made recently?" in page.text
    assert "creates an Ask thread; recordings and notes stay unchanged" in page.text

    catalog = client.get("/api/ask/skills?scope=library").json()["skills"]
    assert all(item["scope"] == "library" for item in catalog)
    assert "Recording, Task" in next(
        item["instruction"] for item in catalog if item["key"] == "task_table"
    )
    response = client.post("/ask/skill", data={"skill_key": "task_table"})
    assert response.status_code == 200
    assert "Cross-recording task table." in response.text
    assert 'data-ask-request data-ask-status="library-ask-status"' in response.text
    assert 'hx-sync="#answer:drop"' in response.text
    assert 'placeholder="Ask a follow-up…"' in response.text
    assert ">Follow up</button>" in response.text
    assert "if(button.disabled)return" in response.text
    assert (
        "if(!Number.isInteger(data.id)||typeof data.title!=='string'||!data.title.trim())"
        in response.text
    )
    assert "error.name==='TypeError'?tr('Could not save')" in response.text
    assert calls[0][1]["file_id"] is None
    assert "across the retrieved recordings" in calls[0][1]["instruction"]

    from localplaud.db.models import AskMessage, AutomationRun, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        messages = list(session.scalars(select(AskMessage).order_by(AskMessage.id)))
        assert {message.skill_key for message in messages} == {"task_table"}
        assert messages[0].skill_snapshot["scope"] == "library"
        assert list(session.scalars(select(UserNote))) == []
        assert list(session.scalars(select(AutomationRun))) == []

    assert client.post("/ask/skill", data={"skill_key": "missing"}).status_code == 404


def test_library_ask_scope_is_durable_and_cannot_change_on_followup(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import AskThread, Folder, PlaudFile, Speaker, Tag
    from localplaud.db.session import session_scope

    with session_scope() as session:
        folder = Folder(name="Research")
        tag = Tag(name="Priority")
        session.add_all([folder, tag])
        session.flush()
        recording = session.get(PlaudFile, "r1")
        recording.folder_id = folder.id
        recording.tags.append(tag)
        session.add(Speaker(file_id="r1", key="SPEAKER_00", display_name="Sky"))
        folder_id, tag_id = folder.id, tag.id

    scopes = []

    def fake_answer(query, **kwargs):
        scopes.append(kwargs.get("retrieval_scope"))
        return {"answer": f"Scoped: {query}", "sources": []}

    monkeypatch.setattr("localplaud.worker.qa.answer", fake_answer)
    page = client.get("/?ask=true")
    assert 'id="library-ask-scope"' in page.text
    assert 'hx-include="#library-ask-scope"' in page.text
    # Scope collapses behind a truthful summary: library-wide until narrowed.
    assert 'id="library-ask-scope-details"' in page.text
    assert 'id="library-ask-scope-summary">Entire library</strong>' in page.text
    assert "ANSWER SCOPE" not in page.text
    assert "`${tr('Custom scope')} · ${active}`" in page.text
    # Suggested prompts and quick actions are scannable, and the quick-action
    # copy states the durable-thread truth rather than claiming read-only.
    assert '<div class="ask-row-label sub">Suggested</div>' in page.text
    assert "Quick actions" in page.text
    assert "creates an Ask thread; recordings and notes stay unchanged" in page.text
    assert "QUICK ACTIONS" not in page.text
    assert 'name="ask_speaker_name"' in page.text and "Sky · 1" in page.text
    first = client.post(
        "/ask",
        data={
            "q": "What changed?",
            "ask_folder_id": str(folder_id),
            "ask_tag_id": str(tag_id),
            "ask_origin": "plaud",
            "ask_speaker_name": "Sky",
            "ask_date_from": "2026-07-01",
            "ask_date_to": "2026-07-31",
            "ask_file_ids": "r1",
        },
    )
    assert first.status_code == 200
    assert "Follow-ups keep this scope." in first.text
    assert "Folder · Research" in first.text and "Tag · Priority" in first.text
    thread_id = _thread_id(first.text)
    expected = {
        "folder_id": folder_id,
        "tag_id": tag_id,
        "origin": "plaud",
        "speaker_name": "Sky",
        "scope_version": 2,
        "date_timezone": "Asia/Taipei",
        "date_from": "2026-07-01",
        "date_from_ms": 1_782_835_200_000,
        "date_to": "2026-07-31",
        "date_to_ms_exclusive": 1_785_513_600_000,
        "file_ids": ["r1"],
    }
    assert scopes == [expected]
    assert "Named speaker · Sky" in first.text
    assert "Timezone · Asia/Taipei" in first.text

    preferences = client.get("/api/preferences/workspace").json()
    assert (
        client.put(
            "/api/preferences/workspace",
            json=preferences | {"timezone": "UTC"},
        ).status_code
        == 200
    )

    followup = client.post("/ask", data={"q": "And next?", "thread_id": thread_id})
    assert followup.status_code == 200
    assert scopes == [expected, expected]
    monkeypatch.setattr(
        "localplaud.worker.qa.answer",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    unavailable = client.post("/ask", data={"q": "Provider down", "thread_id": thread_id})
    assert unavailable.status_code == 200
    assert "Timezone · Asia/Taipei" in unavailable.text
    changed = client.post(
        "/ask",
        data={"q": "Change scope", "thread_id": thread_id, "ask_origin": "local"},
    )
    assert changed.status_code == 409
    assert changed.text.strip() and "<" not in changed.text
    unknown_speaker = client.post(
        "/ask", data={"q": "Unknown", "ask_speaker_name": "Not a named speaker"}
    )
    assert unknown_speaker.status_code == 409
    assert unknown_speaker.text.strip() and "<" not in unknown_speaker.text
    with session_scope() as session:
        assert session.get(AskThread, thread_id).retrieval_scope == expected


def test_legacy_ask_date_scope_remains_utc_after_workspace_timezone_change(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import AskThread
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            AskThread(
                id="legacy-date-thread",
                file_id=None,
                title="Legacy dates",
                retrieval_scope={
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-31",
                },
            )
        )

    preferences = client.get("/api/preferences/workspace").json()
    assert (
        client.put(
            "/api/preferences/workspace",
            json=preferences | {"timezone": "America/New_York"},
        ).status_code
        == 200
    )
    scopes = []
    monkeypatch.setattr(
        "localplaud.worker.qa.answer",
        lambda query, **kwargs: (
            scopes.append(kwargs["retrieval_scope"]) or {"answer": "Legacy answer", "sources": []}
        ),
    )

    response = client.post("/ask", data={"q": "Follow up", "thread_id": "legacy-date-thread"})
    assert response.status_code == 200
    assert scopes == [
        {
            "scope_version": 1,
            "date_timezone": "UTC",
            "date_from": "2026-07-01",
            "date_from_ms": 1_782_864_000_000,
            "date_to": "2026-07-31",
            "date_to_ms_exclusive": 1_785_542_400_000,
        }
    ]
    assert "Timezone · UTC · legacy" in response.text
    with session_scope() as session:
        assert session.get(AskThread, "legacy-date-thread").retrieval_scope == {
            "date_from": "2026-07-01",
            "date_to": "2026-07-31",
        }


def test_invalid_ask_date_scope_never_reaches_provider(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    calls = []
    monkeypatch.setattr(
        "localplaud.worker.qa.answer",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    response = client.post(
        "/ask",
        data={"q": "Unsafe date", "ask_date_to": "9999-12-31"},
    )
    assert response.status_code == 409
    assert "supported range" in response.text
    assert calls == []
