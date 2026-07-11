"""Durable grounded Ask follow-ups and saved-note workflow."""

from __future__ import annotations

import re

from sqlalchemy import create_engine, inspect, select, text


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'ask.db'}")
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
    first = migrate_ask_provenance_schema(engine)
    assert "ask_messages.skill_key" in first
    assert "ask_messages.skill_snapshot" in first
    assert migrate_ask_provenance_schema(engine) == []
    columns = {item["name"] for item in inspect(engine).get_columns("ask_messages")}
    assert {"skill_key", "skill_snapshot"} <= columns


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
    second = client.post(
        "/file/r1/ask", data={"q": "Who confirmed it?", "thread_id": thread_id}
    )
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
    assert client.post(
        "/ask", data={"q": "wrong scope", "thread_id": thread_id}
    ).status_code == 409


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

    notes_page = client.get("/notes")
    assert "The team decided to ship." in notes_page.text
    assert "Weekly Sync" in notes_page.text
    detail = client.get("/file/r1")
    assert "What was decided?" in detail.text
    assert f'data-panel="saved-{note_id}"' in detail.text

    changed = client.put(
        f"/api/notes/{note_id}",
        json={"title": "Launch decision", "content_md": "Edited grounded note."},
    )
    assert changed.status_code == 200
    assert client.get("/api/notes?file_id=r1").json()["notes"][0]["title"] == "Launch decision"
    exported = client.get(f"/api/notes/{note_id}/export.md")
    assert exported.status_code == 200
    assert "# Launch decision" in exported.text
    assert "- Weekly Sync @ 00:12" in exported.text
    assert client.delete(f"/api/notes/{note_id}").status_code == 204
    assert client.get("/api/notes").json()["notes"] == []


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
    assert "Library · ask" in client.get("/notes").text


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

    assert client.post(
        "/file/r1/ask/skill", data={"skill_key": "missing"}
    ).status_code == 404
    assert client.post(
        "/file/missing/ask/skill", data={"skill_key": "task_table"}
    ).status_code == 404
