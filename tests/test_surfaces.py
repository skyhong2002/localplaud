"""Library surfaces: /ask chat page, search, templates, discover, settings."""

from __future__ import annotations

import re
from pathlib import Path


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'surfaces.db'}")
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


def _fake_answer(query, **kwargs):
    return {
        "answer": f"Sky will draft it [1] and Alex reviews [2]. Unknown [9]. ({query})",
        "sources": [
            {"file_id": "r1", "filename": "Weekly Sync", "start": 12.0, "end": 14.0,
             "text": "Sky will prepare the draft", "speaker": "Sky", "target": "transcript"},
            {"file_id": "r2", "filename": "Interview", "start": 4.0, "end": 6.0,
             "text": "Alex will review it", "speaker": None, "target": "transcript"},
        ],
    }


# --------------------------------------------------------------------------- #
# Ask
# --------------------------------------------------------------------------- #


def test_ask_page_renders_empty_chat_with_scope_and_quick_actions(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    page = client.get("/ask")
    assert page.status_code == 200
    html = page.text
    assert 'data-surface="ask"' in html
    assert 'data-sf-ask-form' in html and 'name="ui" value="chat"' in html
    assert "What decisions were made recently?" in html
    assert 'data-sf-skill="task_table"' in html
    assert "creates an Ask thread; recordings and notes stay unchanged" in html
    # Scope selector: All / Folder / Selected files / Date range, bound to the form.
    for kind in ("all", "folder", "files", "dates"):
        assert f'name="sf_scope_kind" value="{kind}"' in html
    assert 'name="ask_folder_id" form="sf-ask-form"' in html
    assert 'name="ask_date_from" form="sf-ask-form"' in html
    assert "/static/js/surfaces.js" in html and "/static/css/surfaces.css" in html
    # The legacy library Ask panel is untouched.
    assert client.get("/?ask=true").status_code == 200


def test_ask_chat_answers_with_numbered_citations_and_grounded_followups(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    monkeypatch.setattr("localplaud.worker.qa.answer", _fake_answer)
    response = client.post("/ask", data={"q": "Who drafts?", "ui": "chat"})
    assert response.status_code == 200
    html = response.text
    thread_id = re.search(r'data-sf-chat data-thread-id="([^"]+)"', html).group(1)
    # Only markers that resolve to a real source become pills.
    assert html.count('class="sf-cite" data-cite="1"') == 1
    assert 'data-cite="2"' in html and 'data-cite="9"' not in html and "[9]" in html
    # Sources panel content links to playable timestamps.
    assert 'href="/file/r1?t=12.0"' in html and "Play from" in html and "0:12" in html
    assert "2 recordings · 2 passages" in html
    assert 'data-sf-save-note="' in html
    # Keep-asking follow-ups are derived from the cited recordings.
    assert "What else was discussed in “Weekly Sync”?" in html
    assert "How do “Weekly Sync” and “Interview” differ?" in html

    page = client.get(f"/ask?thread={thread_id}")
    assert page.status_code == 200
    assert f'name="thread_id" value="{thread_id}"' in page.text
    assert 'aria-current="page"' in page.text
    assert "Follow-ups keep this scope." in page.text

    follow = client.post("/ask", data={"q": "And then?", "thread_id": thread_id, "ui": "chat"})
    assert follow.status_code == 200
    assert follow.text.count('class="sf-msg-user"') == 2

    # Without ui=chat the legacy fragment is still returned.
    legacy = client.post("/ask", data={"q": "Legacy?"})
    assert 'hx-sync="#answer:drop"' in legacy.text


def test_ask_chat_unavailable_provider_is_an_actionable_degraded_state(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()

    def broken(*_args, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("localplaud.worker.qa.answer", broken)
    response = client.post("/ask", data={"q": "Anything?", "ui": "chat"})
    assert response.status_code == 200
    assert "data-sf-unavailable" in response.text
    assert 'data-sf-retry="Anything?"' in response.text
    assert 'href="/settings#connections"' in response.text
    assert "Save as note" not in response.text

    skill = client.post("/ask/skill", data={"skill_key": "task_table", "ui": "chat"})
    assert skill.status_code == 200 and "data-sf-unavailable" in skill.text


def test_ask_page_scoped_skill_and_missing_thread(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    monkeypatch.setattr("localplaud.worker.qa.answer", _fake_answer)
    response = client.post(
        "/ask/skill",
        data={"skill_key": "action_items", "ui": "chat", "ask_file_ids": ["r1", "r2"]},
    )
    assert response.status_code == 200
    assert "2 recordings</span>" in response.text
    missing = client.get("/ask?thread=does-not-exist")
    assert missing.status_code == 200
    assert "That conversation no longer exists" in missing.text


def test_ask_readiness_reports_index_and_llm_health(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    monkeypatch.setattr(
        "localplaud.api.surfaces._llm_health",
        lambda: {"provider": "ollama", "ok": False, "detail": "cannot reach"},
    )
    data = client.get("/api/ask/readiness").json()
    assert data == {
        "indexed_recordings": 0,
        "total_recordings": 2,
        "llm": {"provider": "ollama", "ok": False, "detail": "cannot reach"},
        "ready": False,
    }


def test_qa_context_numbers_excerpts_for_inline_citations():
    from localplaud.worker.qa import _QA_SYSTEM, _format_context

    context = _format_context(
        [
            {"filename": "One", "start": 3.0, "speaker": "Sky", "text": "alpha"},
            {"filename": "Two", "target": "saved_note", "label": "Saved note", "text": "beta"},
        ]
    )
    assert context.startswith("[1] [One @ 3s · Sky] alpha")
    assert "[2] [Two · Saved note] beta" in context
    assert "bracketed numbers" in _QA_SYSTEM


def test_surface_helpers_escape_and_highlight():
    from localplaud.api.surfaces import ask_cite, highlight

    assert str(highlight("<b>Roadmap</b> review", "roadmap")) == (
        "&lt;b&gt;<mark>Roadmap</mark>&lt;/b&gt; review"
    )
    windowed = str(highlight("x" * 300 + " needle " + "y" * 300, "needle", limit=120))
    assert windowed.startswith("…") and "<mark>needle</mark>" in windowed
    assert 'data-cite="1"' in str(ask_cite("<p>a [1] b</p>", 1))
    assert str(ask_cite('<a title="[1]">x</a>', 1)) == '<a title="[1]">x</a>'


def test_surfaces_script_strings_are_translated():
    """Every tr('...') literal in surfaces.js has a zh-TW translation."""
    from localplaud.i18n import catalog

    source = (
        Path(__file__).parents[1] / "src/localplaud/api/static/js/surfaces.js"
    ).read_text(encoding="utf-8")
    keys = set(re.findall(r"\btr\('([^']+)'\)", source))
    for left, right in re.findall(r"tr\([^()]*?\?\s*'([^']+)'\s*:\s*'([^']+)'\)", source):
        keys.update((left, right))
    keys.update(re.findall(r"labels = \[([^\]]+)\]", source) and re.findall(
        r"'([^']+)'", re.findall(r"labels = \[([^\]]+)\]", source)[0]
    ))
    missing = sorted(keys - catalog("zh-Hant-TW").keys())
    assert missing == []
