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
    assert "What decisions were made recently?" not in html
    assert "Check processing and index status" in html
    assert 'href="/status"' in html
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
    from localplaud.ask_skills import list_ask_skills
    from localplaud.i18n import catalog

    source = (
        Path(__file__).parents[1] / "src/localplaud/api/static/js/surfaces.js"
    ).read_text(encoding="utf-8")
    keys = set(re.findall(r"\btr\('([^']+)'\)", source))
    for skill in list_ask_skills("library"):
        keys.update((skill["name"], skill["description"]))
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
    # Recently used carousel: prev/next controls tied to the strip, no native scrollbar.
    recent = html.split('id="sf-recent-templates"', 1)[1].split("</section>", 1)[0]
    assert 'data-sf-carousel-wrap' in recent
    assert 'aria-controls="sf-recent-carousel"' in recent
    assert 'aria-label="Previous templates"' in recent and 'aria-label="Next templates"' in recent
    css = (Path(__file__).parents[1] / "src/localplaud/api/static/css/surfaces.css").read_text()
    assert ".sf-carousel-wrap .sf-carousel { scrollbar-width:none; }" in css


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
    from localplaud.db.models import AutomationRun
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(AutomationRun(rule_id=local_id, rule_version=1, file_id="r1",
                                  status="completed", detail={"rule_name": "Original interview rule"}))
    html = client.get("/discover").text
    history = html.split('id="run-history"', 1)[1]
    assert "Original interview rule" in history
    assert f"rule #{local_id}" not in history
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


def test_discover_next_run_describes_event_trigger_not_a_time(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    rules = {}
    for name, trigger in (("Any", {}), ("Matching", {"title_contains": "interview"}),
                          ("Off", {})):
        response = client.post("/api/automations/rules", json={
            "name": name, "trigger": trigger, "actions": {"note_template_key": "plaud-interview"}})
        assert response.status_code == 201
        rules[name] = response.json()["id"]
    assert client.post(f"/api/automations/rules/{rules['Off']}/toggle").status_code == 200
    external = client.put("/api/automations/external-rules", json={
        "owner_key": "mobile", "owner_label": "Mobile app", "external_id": "x-2",
        "name": "Mirrored off", "enabled": False, "trigger": {},
        "actions": {"note_template_key": "plaud-interview"}})
    assert external.status_code == 200

    def card(html, rule_id):
        return html.split(f'data-rule-id="{rule_id}"', 1)[1].split("</article>", 1)[0]

    html = client.get("/discover").text
    assert "Next run: the next new recording<" in card(html, rules["Any"])
    assert "Next run: the next new recording that matches" in card(html, rules["Matching"])
    assert "Paused · runs again after you enable it" in card(html, rules["Off"])
    assert "Paused by its owner" in card(html, external.json()["rule"]["id"])
    prefs = client.get("/api/preferences/workspace").json() | {"locale": "zh-Hant-TW"}
    assert client.put("/api/preferences/workspace", json=prefs).status_code == 200
    zh = client.get("/discover").text
    assert "下次執行：下一份新錄音<" in card(zh, rules["Any"])
    assert "已由擁有者暫停" in card(zh, external.json()["rule"]["id"])


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def test_settings_groups_sections_with_phone_index_and_profile_stats(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    html = client.get("/settings").text
    assert 'data-surface="settings"' in html
    groups = re.findall(r'data-sf-group="([a-z]+)"', html)
    assert groups == ["account", "workspace", "preferences", "processing", "data",
                      "integrations", "help"]
    for group in groups:
        assert f'href="#group-{group}" data-sf-open-group="{group}"' in html
    # Every legacy anchor still exists and is reachable from the section nav.
    for target in ("plaud-account", "access-security", "workspace-preferences", "vocabulary",
                   "note-templates", "hardware-profiles", "connections", "model-catalog",
                   "execution-profiles", "remote-workers", "private-backup", "privacy",
                   "webhook-integrations", "email-integrations", "automation",
                   "system-health", "support-about"):
        assert f'id="{target}"' in html
        assert f'href="#{target}"' in html
    for hidden_form in ("create-connection", "create-model", "create-profile", "folder-profiles"):
        assert f'id="{hidden_form}"' in html
    assert "Local-only profiles never fall back to a cloud or remote provider." in html
    assert "<dt>Recordings</dt><dd>2</dd>" in html
    assert "data-sf-settings-back hidden" in html


# --------------------------------------------------------------------------- #
# Streaming Ask
# --------------------------------------------------------------------------- #


def _sse_events(text: str) -> list[tuple[str, dict]]:
    import json

    events = []
    for block in text.split("\n\n"):
        name = next((line[6:].strip() for line in block.splitlines() if line.startswith("event:")), None)
        data = "".join(line[5:].strip() for line in block.splitlines() if line.startswith("data:"))
        if name and data:
            events.append((name, json.loads(data)))
    return events


class _StreamingLLM:
    name = "fake"

    def __init__(self, pieces):
        self.pieces = pieces

    def complete(self, prompt, system=None, temperature=0.3, max_tokens=2048, json_schema=None):
        return "".join(self.pieces)

    def stream(self, prompt, system=None, temperature=0.3, max_tokens=2048):
        yield from self.pieces


def _fake_retrieval(monkeypatch, llm):
    import localplaud.worker.qa as qa

    hits = [{"file_id": "r1", "filename": "Weekly Sync", "start": 12.0, "end": 14.0,
             "text": "Sky will prepare the draft", "speaker": "Sky", "target": "transcript",
             "score": 0.9}]
    monkeypatch.setattr(qa, "_retrieve_with_profile", lambda *a, **k: (hits, {}, {}, 0.0))
    monkeypatch.setattr(qa, "_dispatch_with_current_evidence", lambda _hits, dispatch: dispatch())
    monkeypatch.setattr(qa, "validate_evidence_fingerprints", lambda *a, **k: None)
    monkeypatch.setattr(
        qa, "candidate_snapshots",
        lambda *_a: [{"stages": {"ask": {"connection": "local:fake", "model": "fake"}}}],
    )
    monkeypatch.setattr(qa, "_candidate_cost", lambda *a, **k: (0.0, {}))
    monkeypatch.setattr(qa, "_settings_for_stage", lambda settings, *_a: settings)
    monkeypatch.setattr(qa, "build_llm", lambda _cfg: llm)


def test_library_ask_stream_emits_sources_deltas_and_persisted_thread(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    _fake_retrieval(monkeypatch, _StreamingLLM(["Sky ", "drafts ", "it [1]."]))
    response = client.post("/ask/stream", data={"q": "Who drafts?", "ui": "chat"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    names = [name for name, _ in events]
    assert names[0] == "start" and names[1] == "sources" and names[-1] == "done"
    assert [data["text"] for name, data in events if name == "delta"] == ["Sky ", "drafts ", "it [1]."]
    assert events[1][1] == {"passages": 1, "recordings": 1}
    done = events[-1][1]
    assert 'class="sf-cite" data-cite="1"' in done["html"]
    from localplaud.db.models import AskMessage
    from localplaud.db.session import session_scope

    with session_scope() as session:
        stored = [row.content for row in session.query(AskMessage).order_by(AskMessage.id)]
    assert stored == ["Who drafts?", "Sky drafts it [1]."]
    # Non-streaming endpoint keeps working with the same provider (no stream hooks).
    assert client.post("/ask", data={"q": "Again?", "thread_id": done["thread_id"]}).status_code == 200


def test_file_ask_stream_falls_back_to_whole_answer_and_renders_workspace_fragment(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path)
    _seed()

    class WholeAnswer:
        name = "whole"

        def complete(self, prompt, system=None, temperature=0.3, max_tokens=2048, json_schema=None):
            return "Whole answer at 0:12."

    _fake_retrieval(monkeypatch, WholeAnswer())
    response = client.post("/file/r1/ask/stream", data={"q": "What happened?"})
    events = _sse_events(response.text)
    deltas = [data["text"] for name, data in events if name == "delta"]
    assert deltas == ["Whole answer at 0:12."]
    assert 'hx-post="/file/r1/ask"' in events[-1][1]["html"]
    assert client.post("/file/missing/ask/stream", data={"q": "x"}).status_code == 404
    assert client.post("/file/r1/ask/stream", data={"q": "  "}).status_code == 422


def test_ask_stream_cancel_stops_before_anything_is_saved(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    import localplaud.api.surfaces as surfaces

    class Cancelling:
        name = "cancelling"

        def stream(self, prompt, system=None, temperature=0.3, max_tokens=2048):
            yield "Partial "
            for cancel in list(surfaces._ACTIVE_STREAMS.values()):
                cancel.set()
            yield "never shown"

        def complete(self, *a, **k):
            raise AssertionError("streaming providers stream")

    _fake_retrieval(monkeypatch, Cancelling())
    events = _sse_events(client.post("/ask/stream", data={"q": "Stop me"}).text)
    assert [name for name, _ in events][-1] == "cancelled"
    assert [data["text"] for name, data in events if name == "delta"] == ["Partial "]
    from localplaud.db.models import AskMessage, AskThread
    from localplaud.db.session import session_scope

    with session_scope() as session:
        assert session.query(AskMessage).count() == 0
        assert session.query(AskThread).count() == 0
    assert client.post("/api/ask/streams/unknown/cancel").json() == {"cancelled": False}


def test_ask_stream_provider_failure_ends_with_unavailable_fragment(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()

    def broken(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr("localplaud.worker.qa.answer", broken)
    events = _sse_events(client.post("/ask/stream", data={"q": "Hello?", "ui": "chat"}).text)
    assert events[-1][0] == "done" and events[-1][1]["unavailable"] is True
    assert "data-sf-unavailable" in events[-1][1]["html"]


def test_activity_heatmap_counts_days_in_workspace_timezone(monkeypatch, tmp_path):
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    _client(monkeypatch, tmp_path)
    from localplaud.api.surfaces import activity_heatmap
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    zone = ZoneInfo("Asia/Taipei")

    def ms(*args):
        return int(datetime(*args, tzinfo=zone).timestamp() * 1000)

    with session_scope() as session:
        session.add_all([
            PlaudFile(id="a", filename="a", start_time_ms=ms(2026, 10, 1, 0, 30)),
            PlaudFile(id="b", filename="b", start_time_ms=ms(2026, 10, 1, 23, 0)),
            PlaudFile(id="c", filename="c", start_time_ms=ms(2026, 9, 29, 9, 0)),
            PlaudFile(id="t", filename="t", start_time_ms=ms(2026, 10, 1, 9, 0), is_trash=True),
        ])
    with session_scope() as session:
        heat = activity_heatmap(session, "Asia/Taipei", weeks=2, today=date(2026, 10, 2))
    assert heat["total"] == 3 and heat["active_days"] == 2
    assert len(heat["weeks"]) == 2 and all(len(week) == 7 for week in heat["weeks"])
    cells = {cell["date"]: cell for week in heat["weeks"] for cell in week}
    assert cells["2026-10-01"]["count"] == 2 and cells["2026-10-01"]["level"] == 4
    assert cells["2026-09-29"]["count"] == 1 and cells["2026-09-29"]["level"] == 1
    assert cells["2026-10-04"]["count"] is None  # future days render as blanks
    assert heat["start"] == "2026-09-21"


def test_phone_settings_index_and_preferences_page(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    html = client.get("/settings").text
    assert 'class="m-topbar sf-settings-topbar"' in html and "data-sf-settings-topback" in html
    assert 'class="sf-heatmap" role="img"' in html and html.count('class="sf-heat-week"') == 40
    for label in ("Current workspace", "Personalization", "Preferences", "Account"):
        assert f'<span class="sf-index-label">{label}</span>' in html
    assert 'id="group-preferences" data-sf-group="preferences" data-sf-phone-only' in html
    assert 'data-sf-pref-toggle="auto_process_new_recordings"' in html
    for anchor in ("#hardware-profiles", "#vocabulary", "#access-security", "/notifications"):
        assert f'class="sf-pref-row" href="{anchor}"' in html


def test_autoflow_sentence_preview_and_reorder(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    ids = []
    for name, priority in (("A", 10), ("B", 20), ("C", 30)):
        response = client.post(
            "/api/automations/rules",
            json={"name": name, "priority": priority, "trigger": {"title_contains": name},
                  "actions": {"note_template_key": "plaud-interview"}},
        )
        ids.append(response.json()["id"])
    external = client.put(
        "/api/automations/external-rules",
        json={"owner_key": "m", "owner_label": "Mobile", "external_id": "e", "name": "E",
              "enabled": True, "priority": 15, "trigger": {},
              "actions": {"note_template_key": "plaud-autopilot"}, "notify": False},
    ).json()["rule"]["id"]

    preview = client.post(
        "/api/automations/sentence-preview",
        json={"trigger": {"title_contains": "standup", "min_duration_minutes": None},
              "actions": {"note_template_key": "plaud-interview", "add_tag_ids": []},
              "notify": True},
    ).json()["sentence"]
    assert "standup" in preview and "採訪" in preview
    assert "plaud-interview" not in preview
    assert client.post("/api/automations/sentence-preview", json={}).status_code == 200

    reordered = client.post("/api/automations/rule-order", json={"rule_ids": [ids[2], ids[0], ids[1]]})
    assert reordered.status_code == 200
    assert [(item["id"], item["priority"]) for item in reordered.json()["rules"]] == [
        (ids[2], 10), (ids[0], 20), (ids[1], 30)
    ]
    assert set(reordered.json()["changed"]) == set(ids)
    rules = {rule["id"]: rule for rule in client.get("/api/automations/rules").json()["rules"]}
    assert rules[ids[2]]["version"] == 2 and rules[external]["priority"] == 15
    assert client.post("/api/automations/rule-order", json={"rule_ids": [external]}).status_code == 409
    assert client.post("/api/automations/rule-order", json={"rule_ids": [ids[0], ids[0]]}).status_code == 422
    assert client.post("/api/automations/rule-order", json={"rule_ids": [999]}).status_code == 404
    html = client.get("/discover").text
    assert html.count("data-sf-drag") == 3 and 'id="sf-rule-data"' in html
    assert "data-sf-rule-sentence" in html


def test_surface_assets_load_once(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    for route in ("/ask", "/templates", "/discover", "/search", "/settings"):
        response = client.get(route)
        assert response.status_code == 200
        assert response.text.count('src="/static/js/surfaces.js') == 1
        assert response.text.count('href="/static/css/surfaces.css') == 1


def test_surface_navigation_rebinds_restored_nodes_without_aborting_live_requests():
    """Execute the real lifecycle against DOM nodes, including cached history flags."""
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the JavaScript lifecycle regression")
    script = Path(__file__).parents[1] / "src/localplaud/api/static/js/surfaces.js"
    harness = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const document = new EventTarget();
let roots = [];
document.querySelectorAll = () => roots;
const window = {};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document, window, AbortController, console,
});
const signals = [];
window.lpSurfaces.initialisers.test = (_root, signal) => signals.push(signal);
roots = [{dataset: {surface: 'test', sfReady: 'true'}}];
document.dispatchEvent(new Event('lp:navigated'));
assert.equal(signals.length, 1);
window.lpSurfaces.init();
document.dispatchEvent(new Event('lp:navigated'));
assert.equal(signals.length, 1);
assert.equal(signals[0].aborted, false);
const failed = new Event('htmx:beforeSwap');
failed.detail = {target: {id: 'app-view'}, shouldSwap: false};
document.dispatchEvent(failed);
assert.equal(signals[0].aborted, false);
const swap = new Event('htmx:beforeSwap');
swap.detail = {target: {id: 'app-view'}};
document.dispatchEvent(swap);
assert.equal(signals[0].aborted, true);
roots = [{dataset: {surface: 'test', sfReady: 'true'}}];
document.dispatchEvent(new Event('htmx:historyRestore'));
assert.equal(signals.length, 2);
assert.equal(signals[1].aborted, false);
roots = [];
document.dispatchEvent(new Event('lp:navigated'));
assert.equal(signals[1].aborted, true);
"""
    subprocess.run([node, "-e", harness, str(script)], check=True, capture_output=True, text=True)


def test_autoflow_resolves_names_in_preview_and_saved_page(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    from localplaud.db.models import Folder, Tag
    from localplaud.db.session import session_scope

    with session_scope() as session:
        folder = Folder(name="QA Research")
        tag = Tag(name="QA Customer")
        session.add_all([folder, tag])
        session.flush()
        folder_id, tag_id = folder.id, tag.id
    body = {
        "trigger": {"folder_id": folder_id, "tag_id": tag_id},
        "actions": {"folder_id": folder_id, "add_tag_ids": [tag_id],
                    "note_template_key": "plaud-interview"},
    }
    assert client.post("/api/automations/rules", json={"name": "QA labels", **body}).status_code == 201
    preview = client.post("/api/automations/sentence-preview", json=body).json()["sentence"]
    assert "QA Research" in preview and "QA Customer" in preview and "採訪" in preview
    assert f"folder #{folder_id}" not in preview
    page = client.get("/discover")
    assert page.status_code == 200 and preview in page.text
