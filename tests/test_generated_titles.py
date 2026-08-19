"""Locally generated recording titles: precedence, provenance, and backfill.

A generated title fills in only where the user has not renamed the recording,
never overrides a manual ``local_title``, and carries provider/model provenance
so it is not mistaken for a user edit or a Plaud-provided name.
"""

from __future__ import annotations


def _init_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'titles.db'}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.db.session import init_db

    init_db()


def test_clean_generated_title_strips_markup_and_caps():
    from localplaud.worker.pipeline import _clean_generated_title

    assert _clean_generated_title("# 「會議：口琴社」  ") == "會議：口琴社"
    assert _clean_generated_title('  "A Quoted Title"\n') == "A Quoted Title"
    assert _clean_generated_title("   ") is None
    assert _clean_generated_title(None) is None
    assert len(_clean_generated_title("x" * 500)) == 200


def test_display_title_precedence_and_source():
    from localplaud.db.models import PlaudFile

    row = PlaudFile(id="abcdef123456789", filename="Plaud name")
    assert row.display_title == "Plaud name"
    assert row.title_source == "plaud"

    row.generated_title = "Generated name"
    assert row.display_title == "Generated name"
    assert row.title_source == "generated"

    row.local_title = "Manual name"
    assert row.display_title == "Manual name"
    assert row.title_source == "manual"

    empty = PlaudFile(id="0123456789abcdef", filename="")
    assert empty.display_title == "0123456789ab"
    assert empty.title_source == "id"


def test_apply_generated_title_sets_provenance_when_unnamed(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    from localplaud.config import get_settings
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _apply_generated_title

    default_template = get_settings().pipeline.summary_template
    with session_scope() as session:
        session.add(PlaudFile(id="rec", filename="20260722_meeting"))
    with session_scope() as session:
        _apply_generated_title(
            session,
            "rec",
            {"title": "# Weekly Sync", "provider": "remote-worker", "model": "qwen3:8b"},
            default_template,
        )
    with session_scope() as session:
        row = session.get(PlaudFile, "rec")
        assert row.generated_title == "Weekly Sync"
        assert row.generated_title_provider == "remote-worker"
        assert row.generated_title_model == "qwen3:8b"
        assert row.generated_title_at is not None
        assert row.display_title == "Weekly Sync"


def test_apply_generated_title_preserves_manual_and_ignores_secondary_template(
    monkeypatch, tmp_path
):
    _init_db(monkeypatch, tmp_path)
    from localplaud.config import get_settings
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _apply_generated_title

    default_template = get_settings().pipeline.summary_template
    with session_scope() as session:
        session.add(PlaudFile(id="manual", filename="raw", local_title="My hand title"))
        session.add(PlaudFile(id="rec2", filename="raw2"))

    with session_scope() as session:
        # Manual rename must never be overwritten.
        _apply_generated_title(
            session, "manual", {"title": "Auto title", "provider": "p", "model": "m"},
            default_template,
        )
        # A non-default note template must not rename the recording.
        _apply_generated_title(
            session, "rec2", {"title": "Auto title", "provider": "p", "model": "m"},
            default_template + "-other",
        )
    with session_scope() as session:
        assert session.get(PlaudFile, "manual").generated_title is None
        assert session.get(PlaudFile, "manual").display_title == "My hand title"
        assert session.get(PlaudFile, "rec2").generated_title is None


def test_backfill_titles_cli_fills_unnamed_only(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    from typer.testing import CliRunner

    from localplaud.cli import app
    from localplaud.config import get_settings
    from localplaud.db.models import PlaudFile
    from localplaud.db.models import Summary as SummaryRow
    from localplaud.db.session import session_scope

    template = get_settings().pipeline.summary_template
    with session_scope() as session:
        session.add(PlaudFile(id="unnamed", filename="raw-1"))
        session.add(PlaudFile(id="named", filename="raw-2", local_title="Kept"))
        for fid in ("unnamed", "named"):
            session.add(
                SummaryRow(
                    file_id=fid,
                    template=template,
                    source="local",
                    title="# Summary Heading",
                    content_md="body",
                    llm_provider="remote-worker",
                    model="qwen3:8b",
                )
            )

    result = CliRunner().invoke(app, ["backfill-titles"])
    assert result.exit_code == 0, result.output
    with session_scope() as session:
        assert session.get(PlaudFile, "unnamed").generated_title == "Summary Heading"
        # Manual rename untouched, and no generated title written over it.
        assert session.get(PlaudFile, "named").generated_title is None
        assert session.get(PlaudFile, "named").display_title == "Kept"


def test_backfill_titles_dry_run_writes_nothing(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    from typer.testing import CliRunner

    from localplaud.cli import app
    from localplaud.config import get_settings
    from localplaud.db.models import PlaudFile
    from localplaud.db.models import Summary as SummaryRow
    from localplaud.db.session import session_scope

    template = get_settings().pipeline.summary_template
    with session_scope() as session:
        session.add(PlaudFile(id="rec", filename="raw"))
        session.add(
            SummaryRow(
                file_id="rec", template=template, source="local",
                title="Heading", content_md="b", llm_provider="p", model="m",
            )
        )
    result = CliRunner().invoke(app, ["backfill-titles", "--dry-run"])
    assert result.exit_code == 0, result.output
    with session_scope() as session:
        assert session.get(PlaudFile, "rec").generated_title is None
