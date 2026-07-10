from __future__ import annotations


def _db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.session import init_db

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'notes.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()


def test_builtin_templates_are_editable_versioned_data(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    _db(monkeypatch, tmp_path)
    from localplaud.api.app import app

    client = TestClient(app)
    templates = client.get("/api/note-templates").json()["templates"]
    assert {row["name"] for row in templates} >= {"default", "meeting", "call", "lecture"}
    default = next(row for row in templates if row["name"] == "default")
    payload = {key: default[key] for key in (
        "name", "system_prompt", "instructions", "description", "category", "language", "enabled"
    )}
    payload["instructions"] += "\n\n## Risks\n- grounded risks"
    updated = client.put(f"/api/note-templates/{default['id']}", json=payload)
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["provenance"] == "user"


def test_safe_note_regeneration_preserves_history_and_provenance(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        SummaryRevision,
        Transcript,
    )
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _persist_summary

    with session_scope() as session:
        session.add(PlaudFile(id="r1", status=FileStatus.done))
        session.add(Transcript(file_id="r1", provider="fake", source="local", text="hello",
                               segments=[{"text": "hello", "start": 0, "end": 1}]))
        session.add(StageRun(file_id="r1", stage=StageName.summarize,
                             profile_snapshot={"route": {"provider": "ollama", "model": "m"}}))
    first = {"template": "default", "title": "One", "content_md": "# One",
             "provider": "ollama", "model": "m", "template_version": 1}
    _persist_summary("r1", first)
    _persist_summary("r1", first | {"title": "Two", "content_md": "# Two", "template_version": 2})
    with session_scope() as session:
        file = session.get(PlaudFile, "r1")
        assert file.summaries[0].revision == 2
        assert file.summaries[0].template_version == 2
        assert file.summaries[0].profile_snapshot["route"]["provider"] == "ollama"
        history = session.query(SummaryRevision).filter_by(file_id="r1").one()
        assert history.revision == 1 and history.content_md == "# One"


def test_template_default_profile_is_a_real_pipeline_resolution_layer(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    from localplaud.db.models import ExecutionProfile, NoteTemplate, PlaudFile, StageName
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _begin_stage

    with session_scope() as session:
        profile = ExecutionProfile(name="template-profile",
                                   stages={"summarize": {"options": {"marker": "template"}}},
                                   policy={})
        session.add(profile)
        session.flush()
        session.query(NoteTemplate).filter_by(name="default").one().execution_profile_id = profile.id
        session.add(PlaudFile(id="template-file"))
    _begin_stage("template-file", StageName.summarize, {"template_name": "default"})
    with session_scope() as session:
        run = session.get(PlaudFile, "template-file").stage_runs[0]
        assert [layer["source"] for layer in run.profile_snapshot["layers"]] == [
            "system_default", "template_default"
        ]
        assert run.profile_snapshot["route"]["options"]["marker"] == "template"
