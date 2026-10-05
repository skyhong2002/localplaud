"""Notes under retired template keys stop shadowing the current note."""

from __future__ import annotations

import pytest

from localplaud.db.models import (
    FileStatus,
    KnowledgeDocument,
    NoteTemplate,
    PlaudFile,
    Summary,
    SummaryRevision,
    UserNote,
)
from localplaud.db.session import session_scope


@pytest.fixture()
def db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'retire.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.db.session import init_db

    init_db()
    yield
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)


def _note(file_id, template, title="t", body="內容", source="local"):
    return Summary(
        file_id=file_id,
        template=template,
        title=title,
        content_md=body,
        llm_provider="p",
        model="m",
        source=source,
    )


def _file(session, file_id="f1"):
    session.add(PlaudFile(id=file_id, filename=file_id, status=FileStatus.done))


def test_retired_key_is_removed_but_everything_else_stays(db):
    from localplaud.note_history import legacy_note_templates, retire_legacy_notes

    with session_scope() as s:
        _file(s)
        s.add_all(
            [
                _note("f1", "default", title="Meeting Coverage Notes", body="English"),
                _note("f1", "plaud-autopilot", title="現行", body="繁體中文"),
                _note("f1", "mind_map", title="", body="# 心智圖"),
                _note("f1", "auto_sum_note", source="cloud"),
                _note("f1", "my-personal", body="自訂"),
            ]
        )
        s.add(
            NoteTemplate(
                key="my-personal",
                version=1,
                name="mine",
                system_prompt="",
                instructions="x",
                is_active=True,
                is_builtin=False,
            )
        )
        s.flush()
        assert legacy_note_templates(s) == {"default"}
        retired = retire_legacy_notes(s, "f1")
        assert len(retired) == 1
    with session_scope() as s:
        live = {(n.template, n.source) for n in s.query(Summary).filter_by(file_id="f1")}
        assert live == {
            ("plaud-autopilot", "local"),
            ("mind_map", "local"),
            ("auto_sum_note", "cloud"),
            ("my-personal", "local"),
        }
        # Normal displacement rule: the outgoing note survives as an archived version.
        versions = s.query(SummaryRevision).filter_by(file_id="f1", template="default").all()
        assert [v.content_md for v in versions] == ["English"]
        assert versions[0].archive_reason == "retired-template"


def test_never_retires_when_the_recording_has_no_current_note(db):
    from localplaud.note_history import retire_legacy_notes

    with session_scope() as s:
        _file(s)
        s.add(_note("f1", "default", body="only note"))
        s.flush()
        assert retire_legacy_notes(s, "f1") == []
    with session_scope() as s:
        assert s.query(Summary).filter_by(file_id="f1", template="default").count() == 1


def test_hard_delete_removes_versions_unlinks_copies_and_honors_only(db):
    from localplaud.note_history import retire_legacy_notes

    with session_scope() as s:
        _file(s)
        legacy = _note("f1", "default", body="old")
        other_legacy = _note("f1", "meeting", body="other retired key")
        s.add_all([legacy, other_legacy, _note("f1", "plaud-autopilot")])
        s.flush()
        s.add(
            SummaryRevision(
                file_id="f1", template="default", revision=1, content_md="older", source="local"
            )
        )
        copy = UserNote(
            file_id="f1", title="我的筆記", content_md="我改過", source_summary_id=legacy.id
        )
        s.add(copy)
        s.flush()
        copy_id = copy.id
        retired = retire_legacy_notes(s, "f1", keep_history=False, only={"default"})
        assert len(retired) == 1
    with session_scope() as s:
        assert s.query(Summary).filter_by(file_id="f1", template="default").count() == 0
        assert s.query(SummaryRevision).filter_by(file_id="f1", template="default").count() == 0
        # A different retired key is outside the explicit operator selection.
        assert s.query(Summary).filter_by(file_id="f1", template="meeting").count() == 1
        kept = s.get(UserNote, copy_id)
        assert kept is not None and kept.content_md == "我改過" and kept.source_summary_id is None


def test_knowledge_documents_of_retired_notes_go_with_them(db):
    from localplaud.note_history import retire_legacy_notes

    with session_scope() as s:
        _file(s)
        legacy = _note("f1", "default")
        s.add_all([legacy, _note("f1", "plaud-autopilot")])
        s.flush()
        s.add(
            KnowledgeDocument(
                file_id="f1",
                summary_id=legacy.id,
                kind="generated_summary",
                status="done",
                content_sha256="0" * 64,
                generation="g1",
            )
        )
        s.flush()
        retire_legacy_notes(s, "f1", keep_history=False, only={"default"})
    with session_scope() as s:
        assert s.query(KnowledgeDocument).filter_by(file_id="f1").count() == 0


def test_persisting_a_current_note_retires_the_legacy_one_but_a_legacy_persist_does_not(db):
    from localplaud.worker.pipeline import _persist_summary

    with session_scope() as s:
        _file(s)
        s.add(_note("f1", "default", body="old English"))
    lineage = {
        "input_transcript_id": None,
        "input_transcript_revision": 0,
        "input_transcript_source": None,
    }
    # Persisting under a retired key must not retire itself.
    _persist_summary("f1", {"template": "default", "content_md": "x", "title": "t"}, lineage)
    with session_scope() as s:
        assert s.query(Summary).filter_by(file_id="f1", template="default").count() == 1
    _persist_summary(
        "f1",
        {"template": "plaud-autopilot", "content_md": "# 繁體\n內容", "title": "繁體"},
        lineage,
    )
    with session_scope() as s:
        templates = {n.template for n in s.query(Summary).filter_by(file_id="f1")}
        assert templates == {"plaud-autopilot"}
        assert s.query(SummaryRevision).filter_by(file_id="f1", template="default").count() >= 1


def test_operator_script_dry_run_refuses_current_keys_and_apply_writes_backup(
    db, tmp_path, monkeypatch
):
    import importlib.util
    import json
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "retire_script", Path(__file__).parents[1] / "scripts/maintenance/retire_legacy_notes.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with session_scope() as s:
        _file(s)
        s.add_all([_note("f1", "default", body="old"), _note("f1", "plaud-autopilot")])
        _file(s, "f2")
        s.add(_note("f2", "default", body="no current note"))

    monkeypatch.setattr(sys, "argv", ["x", "--template", "plaud-autopilot"])
    assert module.main() == 2
    monkeypatch.setattr(sys, "argv", ["x", "--template", "default"])
    assert module.main() == 0
    with session_scope() as s:
        assert s.query(Summary).filter_by(template="default").count() == 2

    monkeypatch.setattr(sys, "argv", ["x", "--template", "default", "--apply"])
    assert module.main() == 0
    backups = list((tmp_path / "backups").glob("*"))
    assert any(p.suffix == ".db" for p in backups)
    export = next(p for p in backups if p.suffix == ".json")
    assert {row["content_md"] for row in json.loads(export.read_text())} >= {
        "old",
        "no current note",
    }
    with session_scope() as s:
        left = {(n.file_id, n.template) for n in s.query(Summary)}
        assert ("f1", "default") not in left
        # A recording without a current note keeps its only note.
        assert ("f2", "default") in left
