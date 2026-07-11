"""Markdown export against a seeded temporary SQLite database."""

import pytest

import localplaud.config as config
import localplaud.db.session as db_session
from localplaud.db.models import PlaudFile, Summary, Transcript
from localplaud.db.session import init_db, session_scope
from localplaud.exporter import export_to_file, render_markdown

FILE_ID = "dab5c6ca728964152f32d93ed76c1950"


def _fresh_db(monkeypatch, tmp_path):
    """Point settings at a tmp sqlite file and reset the engine singletons."""
    db_file = tmp_path / "localplaud-test.db"
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.chdir(tmp_path)  # avoid picking up a real config.toml/.env
    config.get_settings(reload=True)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)


@pytest.fixture
def seeded_db(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    init_db()
    with session_scope() as session:
        f = PlaudFile(
            id=FILE_ID,
            filename="2026-07-09 15:38:57",
            start_time_ms=1783582737000,
            duration_ms=2489000,
        )
        f.transcript = Transcript(
            provider="dummy",
            has_speakers=True,
            text="hello there\ngeneral kenobi",
            segments=[
                {"text": "hello there", "start": 0.0, "end": 1.5, "speaker": "SPEAKER_00"},
                {"text": "general kenobi", "start": 65.2, "end": 67.0, "speaker": "SPEAKER_01"},
            ],
        )
        f.summaries = [
            Summary(template="default", title="A Chat", content_md="# A Chat\n\nShort chat."),
            Summary(template="meeting", title="Standup", content_md="# Standup\n\nDecisions."),
        ]
        session.add(f)
    return tmp_path


def test_render_markdown_contains_everything(seeded_db):
    md = render_markdown(FILE_ID)
    assert "# 2026-07-09 15:38:57" in md
    assert "## Default: A Chat" in md
    assert "## Meeting: Standup" in md
    assert "## Transcript" in md
    assert "**[00:00] SPEAKER_00:** hello there" in md
    assert "**[01:05] SPEAKER_01:** general kenobi" in md


def test_render_markdown_missing_file_raises(seeded_db):
    with pytest.raises(ValueError):
        render_markdown("nope")


def test_render_markdown_handles_bare_file(seeded_db):
    with session_scope() as session:
        session.add(PlaudFile(id="bare", filename="no extras"))
    md = render_markdown("bare")
    assert "# no extras" in md
    assert "## Transcript" not in md


def test_export_to_file_writes_default_path(seeded_db):
    path = export_to_file(FILE_ID)
    assert path.name == "export.md"
    assert path.parent.name == FILE_ID
    assert "## Transcript" in path.read_text(encoding="utf-8")


def test_export_to_file_explicit_dest(seeded_db, tmp_path):
    dest = tmp_path / "out" / "note.md"
    path = export_to_file(FILE_ID, dest)
    assert path == dest
    assert dest.exists()


def test_independent_export_excludes_imported_plaud_artifacts(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    init_db()
    with session_scope() as session:
        file = PlaudFile(id="imported", filename="Imported only")
        file.transcripts = [
            Transcript(
                provider="plaud",
                source="cloud",
                text="paid transcript",
                segments=[{"text": "paid transcript", "start": 0.0, "end": 1.0}],
            )
        ]
        file.summaries = [
            Summary(template="plaud", source="cloud", content_md="paid note")
        ]
        session.add(file)

    md = render_markdown("imported")
    assert "# Imported only" in md
    assert "paid transcript" not in md
    assert "paid note" not in md
