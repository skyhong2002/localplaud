"""Operator repair for notes stored as a JSON envelope instead of Markdown."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from localplaud.db.models import FileStatus, PlaudFile, Summary, SummaryRevision
from localplaud.db.session import session_scope

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "maintenance" / "unwrap_json_notes.py"


@pytest.fixture()
def db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'unwrap.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.db.session import init_db

    init_db()
    yield tmp_path
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)


@pytest.fixture()
def script():
    spec = importlib.util.spec_from_file_location("unwrap_json_notes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["unwrap_json_notes"] = module
    spec.loader.exec_module(module)
    return module


def _row(template, source, body, title="t"):
    return Summary(
        file_id="f1", template=template, title=title, content_md=body, source=source,
        llm_provider="ollama" if source == "local" else None,
        model="qwen3:8b" if source == "local" else None,
    )


def _seed():
    cloud = json.dumps({"ai_content": "## 摘要\n- 重點", "category": "x", "state": 10})
    local = json.dumps(
        {"title": "里長選舉爭議摘要", "content_md": "# 里長選舉爭議摘要\n\n內容"},
        ensure_ascii=False,
    )
    transcripty = json.dumps({"title": "x", "content_md": "SPEAKER_00: 你好"}, ensure_ascii=False)
    with session_scope() as s:
        s.add(PlaudFile(id="f1", filename="f1", status=FileStatus.done))
        s.add_all(
            [
                _row("auto_sum_note", "cloud", cloud),
                _row("outline", "cloud", "- [0:00] 章節"),
                _row("plaud-autopilot", "local", local, title=None),
                _row("meeting", "local", "# 已是 Markdown\n\n不要動"),
                _row("notes", "local", transcripty),
                _row("cut", "local", '{\n  "title": "問卷設計",\n  "content_md": "# 問卷設計\\n\\n- 將研究問題轉化為具體'),
                _row("blob", "local", '{"unrelated": true}'),
            ]
        )


def _bodies():
    with session_scope() as s:
        return {r.template: (r.content_md, r.title) for r in s.query(Summary)}


def test_dry_run_changes_nothing_and_flags_transcript_bodies(db, script, monkeypatch, capsys):
    _seed()
    before = _bodies()
    monkeypatch.setattr(sys, "argv", ["unwrap_json_notes.py"])
    assert script.main() == 0
    out = capsys.readouterr().out
    assert "rewritable: 2 (cloud 1, local 1); needs regeneration instead: 2" in out
    assert "inner body is transcript text" in out
    assert "truncated JSON envelope" in out
    assert _bodies() == before
    assert not (db / "backups").exists()


def test_apply_unwraps_backs_up_and_archives_local_original(db, script, monkeypatch):
    _seed()
    local_original = _bodies()["plaud-autopilot"][0]
    monkeypatch.setattr(sys, "argv", ["unwrap_json_notes.py", "--apply"])
    assert script.main() == 0

    after = _bodies()
    assert after["auto_sum_note"][0] == "## 摘要\n- 重點"
    assert after["plaud-autopilot"] == ("# 里長選舉爭議摘要\n\n內容", "里長選舉爭議摘要")
    # Untouched: real Markdown, other JSON, cloud outline, and the transcript-text body.
    assert after["meeting"][0] == "# 已是 Markdown\n\n不要動"
    assert after["blob"][0] == '{"unrelated": true}'
    assert after["outline"][0] == "- [0:00] 章節"
    assert after["notes"][0].startswith('{"title"')
    assert after["cut"][0].startswith('{\n  "title"')  # truncated: left for regeneration

    with session_scope() as s:
        versions = list(s.query(SummaryRevision))
    assert [(v.template, v.content_md, v.archive_reason) for v in versions] == [
        ("plaud-autopilot", local_original, "unwrap-json-envelope")
    ]
    backups = list((db / "backups").iterdir())
    assert {p.name.split("-")[0] for p in backups} == {"localplaud", "unwrapped"}
    exported = json.loads(next(p for p in backups if p.suffix == ".json").read_text())
    assert {item["template"] for item in exported} == {"auto_sum_note", "plaud-autopilot"}
    assert next(i for i in exported if i["template"] == "auto_sum_note")["content_md"].startswith(
        '{"ai_content"'
    )

    # Idempotent: a second run finds nothing left to rewrite.
    assert script.main() == 0
    with session_scope() as s:
        assert s.query(SummaryRevision).count() == 1
