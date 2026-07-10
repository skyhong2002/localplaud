from __future__ import annotations


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'ask.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def test_followup_thread_and_save_answer_to_note(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="r1", filename="Interview"))
    calls = []

    def fake_answer(question, file_id=None, history=None, **kwargs):
        calls.append((question, file_id, history))
        return {"answer": f"Grounded: {question}", "sources": [{
            "file_id": "r1", "filename": "Interview", "start": 12.5, "text": "source",
            "score": 0.9,
        }], "provider": "ollama", "model": "local-model",
                "profile_snapshot": {"route": {"provider": "ollama"}}}

    monkeypatch.setattr("localplaud.worker.qa.answer", fake_answer)
    thread = client.post("/api/ask/threads", json={"file_id": "r1", "title": "Decisions"})
    assert thread.status_code == 201
    thread_id = thread.json()["id"]
    first = client.post(f"/api/ask/threads/{thread_id}/messages",
                        json={"question": "What was decided?"})
    assert first.status_code == 201
    second = client.post(f"/api/ask/threads/{thread_id}/messages",
                         json={"question": "Who owns it?"})
    assert second.status_code == 201
    assert calls[1][2] == [
        {"role": "user", "content": "What was decided?"},
        {"role": "assistant", "content": "Grounded: What was decided?"},
    ]
    saved = client.post(f"/api/ask/messages/{second.json()['id']}/save-note", json={})
    assert saved.status_code == 201
    assert saved.json()["kind"] == "ask-answer"
    assert "/file/r1?t=12.5" in saved.json()["content_md"]
    assert saved.json()["provenance"]["profile_snapshot"]["route"]["provider"] == "ollama"
