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


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


def _seed_transcript():
    from localplaud.db.models import FileStatus, PlaudFile, Transcript
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            PlaudFile(
                id="r3",
                filename="Roadmap review",
                status=FileStatus.done,
                start_time_ms=1_790_000_000_000,
                duration_ms=60_000,
            )
        )
        session.flush()
        session.add(
            Transcript(
                file_id="r3",
                provider="test",
                source="local",
                text="We will ship the <roadmap> in Q4",
                segments=[
                    {"text": "We will ship the <roadmap> in Q4", "start": 7.5, "end": 9.0}
                ],
            )
        )


def test_search_page_highlights_hits_and_hands_query_to_ask(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed_transcript()
    monkeypatch.setattr("localplaud.worker.qa.retrieve", lambda *args, **kwargs: [])
    page = client.get("/search", params={"q": "roadmap"})
    assert page.status_code == 200
    html = page.text
    assert 'data-surface="search"' in html
    assert 'hx-get="/search"' in html and 'hx-select="#sf-search-results"' in html
    assert 'href="/ask?q=roadmap&amp;send=1"' in html
    assert "<mark>Roadmap</mark> review" in html
    # Transcript text is escaped before highlighting.
    assert "&lt;<mark>roadmap</mark>&gt;" in html
    assert 'href="/file/r3?t=7.5"' in html
    for kind in ("all", "files", "content"):
        assert f'name="sf_kind" value="{kind}"' in html
    empty = client.get("/search")
    assert "data-sf-recent" in empty.text and 'href="/ask"' in empty.text


def test_search_api_returns_flat_best_matches_without_semantic_by_default(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path)
    _seed_transcript()
    calls = []
    monkeypatch.setattr(
        "localplaud.worker.qa.retrieve", lambda *args, **kwargs: calls.append(kwargs) or []
    )
    data = client.get("/api/search", params={"q": "roadmap"}).json()
    assert calls == []
    first = data["results"][0]
    assert first["file_id"] == "r3"
    assert first["href"] == "/file/r3?t=7.5"
    assert first["kind"] == "Transcript"
    assert "<roadmap>" in first["snippet"]
    client.get("/api/search", params={"q": "roadmap", "semantic": "true"})
    assert len(calls) == 1
    assert client.get("/api/search").json()["results"] == []


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


def test_templates_page_shows_recent_usage_personal_and_catalog(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    created = client.post(
        "/api/note-templates",
        json={"key": "my-standup", "name": "Stand-up digest", "system_prompt": "",
              "instructions": "## Yesterday\n## Today\n## Blockers"},
    )
    assert created.status_code == 201
    from localplaud.db.models import PlaudFile, Summary
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="r9", filename="Cloud copy"))
        session.flush()
        session.add_all(
            [Summary(file_id="r1", template="my-standup", source="local", content_md="a"),
             Summary(file_id="r2", template="my-standup", source="local", content_md="b"),
             Summary(file_id="r9", template="my-standup", source="cloud", content_md="c")]
        )
    mine = client.get("/templates")
    assert mine.status_code == 200
    html = mine.text
    assert "Recently used" in html and "Stand-up digest" in html
    assert "Mine · v1" in html
    # Only local notes count as usage; cloud artifacts are not local provenance.
    assert 'title="Notes generated with this template">' in html
    assert re.search(r"Notes generated with this template\">.*?</svg> 2|"
                     r"Notes generated with this template\">.*?</span> 2", html, re.S)
    assert 'data-sf-template-create' in html
    explore = client.get("/templates?tab=explore")
    assert "Most used in this workspace" not in explore.text  # no built-in usage yet
    assert "Discover lists the templates bundled with this workspace" in explore.text
    assert "Add to My templates" in explore.text
    category = client.get("/templates?tab=explore&category=Work")
    assert 'aria-current="true"' in category.text
    searched = client.get("/templates?tab=my&q=stand-up")
    assert "Stand-up digest" in searched.text


# --------------------------------------------------------------------------- #
# Discover / AutoFlow
# --------------------------------------------------------------------------- #


def test_discover_cards_distinguish_editable_and_external_rules(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    local = client.post(
        "/api/automations/rules",
        json={"name": "Interviews", "trigger": {"title_contains": "interview"},
              "actions": {"note_template_key": "plaud-interview"}, "notify": True},
    )
    assert local.status_code == 201
    local_id = local.json()["id"]
    external = client.put(
        "/api/automations/external-rules",
        json={"owner_key": "mobile", "owner_label": "Mobile app", "external_id": "x-1",
              "name": "Mirrored", "enabled": True, "priority": 50, "trigger": {},
              "actions": {"note_template_key": "plaud-autopilot"}, "notify": False},
    )
    assert external.status_code == 200
    external_id = external.json()["rule"]["id"]
    html = client.get("/discover").text
    # Local rules: enable switch, edit, delete.
    assert f'class="rule-toggle" data-id="{local_id}" checked' in html
    assert f'class="btn sec btn-sm rule-edit" data-id="{local_id}"' in html
    assert 'title="inbox notification enabled"' in html
    # External rules: read-only lock, view-only editor, no toggle or delete.
    assert f'class="btn sec btn-sm rule-view" data-id="{external_id}"' in html
    assert f'class="rule-toggle" data-id="{external_id}"' not in html
    assert f'rule-delete" data-id="{external_id}"' not in html
    assert 'title="Managed by Mobile app">Read-only</span>' in html
    assert "function setReadOnly(rule)" in html
    assert 'id="rule-readonly-note" hidden' in html
    # When / Then editor keeps every existing field.
    for name in ("origin", "title_contains", "transcript_contains", "min_duration_minutes",
                 "note_template_key", "profile_id", "action_folder_id", "notify"):
        assert f'name="{name}"' in html
    assert 'id="run-history"' in html and 'id="integrations"' in html
