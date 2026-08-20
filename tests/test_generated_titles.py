"""Locally generated recording titles: invariant, precedence, and backfill.

Every local transcript gets a generated-title artifact. It never overrides the
display precedence of a manual ``local_title`` and carries provider/model
provenance so it is not mistaken for a user edit or a Plaud-provided name.
"""

from __future__ import annotations


def _init_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'titles.db'}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.db.session import init_db

    init_db()


def test_clean_generated_title_strips_markup_and_caps():
    from localplaud.worker.pipeline import _clean_generated_title, _generated_title_candidate

    assert _clean_generated_title("# 「會議：口琴社」  ") == "會議：口琴社"
    assert _clean_generated_title('  "A Quoted Title"\n') == "A Quoted Title"
    assert _clean_generated_title("眾人驚呼「哇，一定是高級」") == "眾人驚呼「哇，一定是高級」"
    assert _clean_generated_title("眾人驚呼「哇，一定是高級") is None
    assert _clean_generated_title("   ") is None
    assert _clean_generated_title(None) is None
    assert _clean_generated_title("x" * 500) is None
    assert _clean_generated_title("以下是根據您提供的內容整理出的摘要") is None
    assert _clean_generated_title("**清楚的會議標題**") == "清楚的會議標題"
    assert _clean_generated_title("Autopilot") is None
    assert _clean_generated_title("Coverage Notes Summary") is None
    assert _clean_generated_title("Transcript Overview") is None
    assert _clean_generated_title("轉錄內容概覽") is None
    assert _clean_generated_title("转录内容概览") is None
    assert _clean_generated_title("轉錄內容摘要") is None
    assert _clean_generated_title("转录内容摘要") is None
    assert _clean_generated_title("Mind map") is None
    assert _clean_generated_title("智能總結") is None
    assert _clean_generated_title("Autopilot 模板") is None
    assert _clean_generated_title("Key Points") is None
    assert _clean_generated_title("自適應結構") is None
    assert _clean_generated_title("Transcript 總結") is None
    assert _clean_generated_title("SPEAKER_02: 安") is None
    assert _clean_generated_title("安安安安安安安安") is None
    assert _clean_generated_title("Tips") is None
    assert _clean_generated_title("Content Summary") is None
    assert _clean_generated_title("會談內容") is None
    assert _clean_generated_title("錄音內容摘要") is None
    assert _clean_generated_title("Meeting summary") is None
    assert _clean_generated_title("討論摘要") is None
    assert _clean_generated_title("讨论摘要") is None
    assert _clean_generated_title("討論內容摘要") is None
    assert _generated_title_candidate(None, "\n# AI heading\nbody") == "AI heading"
    assert _generated_title_candidate(None, "## Generated heading\nbody") == "Generated heading"
    assert _generated_title_candidate(None, "Generated first line\nbody") is None


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


def test_apply_generated_title_preserves_manual_display_and_ignores_secondary_template(
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
        session.add(PlaudFile(id="auto", filename="raw3"))

    with session_scope() as session:
        # Manual display rename stays preferred, but the AI artifact is stored.
        _apply_generated_title(
            session,
            "manual",
            {"title": "Auto title", "provider": "p", "model": "m"},
            default_template,
        )
        # A non-default note template must not rename the recording.
        _apply_generated_title(
            session,
            "rec2",
            {"title": "Auto title", "provider": "p", "model": "m"},
            default_template + "-other",
        )
        _apply_generated_title(
            session,
            "auto",
            {
                "title": None,
                "content_md": "# Auto-selected heading\nbody",
                "provider": "p",
                "model": "m",
            },
            default_template + "-auto",
            primary_summary=True,
        )
    with session_scope() as session:
        assert session.get(PlaudFile, "manual").generated_title == "Auto title"
        assert session.get(PlaudFile, "manual").display_title == "My hand title"
        assert session.get(PlaudFile, "rec2").generated_title is None
        assert session.get(PlaudFile, "auto").generated_title == "Auto-selected heading"


def test_backfill_titles_cli_fills_every_local_transcript(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    from typer.testing import CliRunner

    from localplaud.cli import app
    from localplaud.config import get_settings
    from localplaud.db.models import PlaudFile
    from localplaud.db.models import Summary as SummaryRow
    from localplaud.db.session import session_scope

    template = get_settings().pipeline.summary_template
    with session_scope() as session:
        from localplaud.db.models import Transcript

        session.add(PlaudFile(id="unnamed", filename="raw-1"))
        session.add(PlaudFile(id="named", filename="raw-2", local_title="Kept"))
        session.add(PlaudFile(id="heading", filename="raw-3"))
        for fid in ("unnamed", "named", "heading"):
            session.add(
                Transcript(
                    file_id=fid,
                    provider="asr",
                    source="local",
                    text="hello",
                    segments=[{"text": "hello", "start": 0, "end": 1}],
                )
            )
            if fid != "heading":
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
        session.add(
            SummaryRow(
                file_id="heading",
                template=template,
                source="local",
                title=None,
                content_md="Generated prose without a Markdown heading.",
                llm_provider="ollama",
                model="qwen3:8b",
            )
        )
        session.add(
            SummaryRow(
                file_id="heading",
                template="mind_map",
                source="local",
                title=None,
                content_md="# AI mind-map heading\n- body",
                llm_provider="ollama",
                model="qwen3:8b",
            )
        )

    result = CliRunner().invoke(app, ["backfill-titles"])
    assert result.exit_code == 0, result.output
    with session_scope() as session:
        assert session.get(PlaudFile, "unnamed").generated_title == "Summary Heading"
        # Manual rename remains visible while the generated artifact also exists.
        assert session.get(PlaudFile, "named").generated_title == "Summary Heading"
        assert session.get(PlaudFile, "named").display_title == "Kept"
        assert session.get(PlaudFile, "heading").generated_title == "AI mind-map heading"


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
        from localplaud.db.models import Transcript

        session.add(PlaudFile(id="rec", filename="raw"))
        session.add(
            Transcript(
                file_id="rec",
                provider="asr",
                source="local",
                text="hello",
                segments=[{"text": "hello", "start": 0, "end": 1}],
            )
        )
        session.add(
            SummaryRow(
                file_id="rec",
                template=template,
                source="local",
                title="Heading",
                content_md="b",
                llm_provider="p",
                model="m",
            )
        )
    result = CliRunner().invoke(app, ["backfill-titles", "--dry-run"])
    assert result.exit_code == 0, result.output
    with session_scope() as session:
        assert session.get(PlaudFile, "rec").generated_title is None
