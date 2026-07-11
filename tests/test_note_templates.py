"""Editable, versioned note templates and per-recording selection."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'notes.db'}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def test_note_template_migration_is_additive_and_idempotent(tmp_path):
    from localplaud.db.migrations import migrate_note_template_schema

    engine = create_engine(f"sqlite:///{tmp_path/'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE summaries (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO plaud_files (id) VALUES ('kept')"))
    assert set(migrate_note_template_schema(engine)) == {
        "plaud_files.note_template_key",
        "summaries.template_version",
        "summaries.template_snapshot",
    }
    assert migrate_note_template_schema(engine) == []
    inspector = inspect(engine)
    assert "note_template_key" in {
        column["name"] for column in inspector.get_columns("plaud_files")
    }
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT id FROM plaud_files")) == "kept"


def test_builtin_bootstrap_and_versioned_crud(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    builtins = client.get("/api/note-templates").json()["templates"]
    assert {row["key"] for row in builtins} >= {"default", "meeting", "call"}
    assert all(row["version"] == 1 for row in builtins)

    created = client.post(
        "/api/note-templates",
        json={
            "key": "research-interview",
            "name": "Research interview",
            "system_prompt": "Stay faithful.",
            "instructions": "# Topic\n\n## Evidence",
        },
    )
    assert created.status_code == 201
    assert client.post(
        "/api/note-templates", json=created.json()
    ).status_code == 409

    version = client.put(
        "/api/note-templates/research-interview",
        json={
            "name": "Research interview",
            "system_prompt": "Stay strictly faithful.",
            "instructions": "# Topic\n\n## Findings\n\n## Evidence",
        },
    )
    assert version.status_code == 201
    assert version.json()["version"] == 2
    active = client.get("/api/note-templates").json()["templates"]
    assert next(row for row in active if row["key"] == "research-interview")["version"] == 2
    history = client.get("/api/note-templates?include_history=true").json()["templates"]
    assert [row["version"] for row in history if row["key"] == "research-interview"] == [2, 1]
    assert client.delete("/api/note-templates/default").status_code == 409


def test_catalog_metadata_and_copy_to_my_space(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    meeting = next(
        row
        for row in client.get("/api/note-templates").json()["templates"]
        if row["key"] == "meeting"
    )
    assert meeting["category"] == "Work"
    assert meeting["scenario"] == "Meetings"
    assert meeting["provenance"] == "first-party"
    copied = client.post(
        "/api/note-templates/meeting/copy",
        json={"key": "my-meeting", "name": "My meeting"},
    )
    assert copied.status_code == 201
    assert copied.json()["provenance"] == "personal-copy"
    assert copied.json()["category"] == "Work"
    assert copied.json()["scenario"] == "Meetings"
    assert copied.json()["description"] == meeting["description"]
    assert copied.json()["system_prompt"] == meeting["system_prompt"]
    assert client.post(
        "/api/note-templates/meeting/copy",
        json={"key": "my-meeting", "name": "Duplicate"},
    ).status_code == 409


def test_templates_workspace_search_and_tabs(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    page = client.get("/templates")
    assert page.status_code == 200
    assert "My Space" in page.text and "Explore" in page.text
    assert "Copy to My Space" in page.text
    assert "Decisions, owners, action items" in page.text
    assert 'id="template-form"' in page.text
    education = client.get("/templates?tab=explore&category=Education")
    assert "Lecture" in education.text and "Meeting" not in education.text
    searched = client.get("/templates?tab=explore&q=voice+memos")
    assert "Personal" in searched.text and "Lecture" not in searched.text


def test_template_discovery_metadata_migration(monkeypatch, tmp_path):
    from localplaud.db.migrations import migrate_note_template_schema

    engine = create_engine(f"sqlite:///{tmp_path/'legacy-template-catalog.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE note_templates (id INTEGER PRIMARY KEY)"))
    migrated = migrate_note_template_schema(engine)
    assert set(migrated) == {
        "note_templates.category",
        "note_templates.scenario",
        "note_templates.description",
        "note_templates.author",
        "note_templates.provenance",
        "note_templates.popularity",
    }
    assert migrate_note_template_schema(engine) == []


def test_recording_selection_marks_notes_stale_and_archive_resets(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="r1", filename="Recording"))
        session.add(
            StageRun(
                file_id="r1",
                stage=StageName.summarize,
                status=StageStatus.completed,
            )
        )
    client.post(
        "/api/note-templates",
        json={
            "key": "custom",
            "name": "Custom",
            "system_prompt": "Faithful notes only.",
            "instructions": "# Note\n\n## Details",
        },
    )
    selected = client.put("/api/files/r1/note-template", json={"key": "custom"})
    assert selected.status_code == 200
    detail = client.get("/file/r1")
    assert detail.status_code == 200
    assert 'id="note-template-select"' in detail.text
    assert "Custom · v1" in detail.text
    with session_scope() as session:
        row = session.get(PlaudFile, "r1")
        assert row.note_template_key == "custom"
        run = next(item for item in row.stage_runs if item.stage == StageName.summarize)
        assert run.status == StageStatus.pending
        assert run.detail["stale"] is True
    assert client.delete("/api/note-templates/custom").status_code == 200
    with session_scope() as session:
        assert session.get(PlaudFile, "r1").note_template_key is None


def test_settings_renders_template_editor(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Note templates" in page.text
    assert 'id="note-template-form"' in page.text
    assert "Default" in page.text


def test_summarize_persists_exact_template_snapshot(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    from localplaud.asr.base import Segment, Transcript
    from localplaud.config import get_settings
    from localplaud.worker import summarize

    class FakeLlm:
        def complete(self, *args, **kwargs):
            return "# Result\n\n## Findings\n- grounded"

    monkeypatch.setattr(summarize, "build_llm", lambda settings: FakeLlm())
    settings = get_settings()
    result = summarize.summarize(
        Transcript(segments=[Segment(start=0, end=1, text="Evidence")]),
        settings,
        template_override={
            "key": "worker-safe",
            "version": 3,
            "name": "Worker safe",
            "system_prompt": "Do not invent.",
            "instructions": "# Result\n\n## Findings",
        },
    )
    assert result["template"] == "worker-safe"
    assert result["template_version"] == 3
    assert result["template_snapshot"]["system_prompt"] == "Do not invent."
