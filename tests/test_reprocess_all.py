"""Whole-library reprocess queueing: modes, filters, dry-run, and eligibility."""

from __future__ import annotations


def _init_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'rp.db'}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.db.session import init_db

    init_db()


def _seed(session):
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
    )

    # done, with a transcript + a completed summarize stage
    done = PlaudFile(id="done1", filename="a", status=FileStatus.done, audio_path="/x/a.opus")
    session.add(done)
    session.add(Transcript(file_id="done1", provider="p", source="local", text="hi", segments=[]))
    session.add(StageRun(file_id="done1", stage=StageName.summarize, attempts=1,
                         status=StageStatus.completed, detail={}))
    # downloaded, no transcript yet
    session.add(PlaudFile(id="dl1", filename="b", status=FileStatus.downloaded, audio_path="/x/b.opus"))
    # no local audio -> ineligible
    session.add(PlaudFile(id="noaudio", filename="c", status=FileStatus.downloaded, audio_path=None))


def test_resume_queues_eligible_and_skips_no_audio(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import queue_library_reprocess

    with session_scope() as session:
        _seed(session)

    result = queue_library_reprocess(mode="resume")
    assert result["queued"] == 2
    assert result["no_audio"] == 1
    with session_scope() as session:
        # done recording with a transcript is re-queued as partial
        assert session.get(PlaudFile, "done1").status == FileStatus.partial
        # unprocessed recording without a transcript is queued as error
        assert session.get(PlaudFile, "dl1").status == FileStatus.error
        assert session.get(PlaudFile, "noaudio").status == FileStatus.downloaded


def test_derived_only_targets_transcribed_and_marks_stale(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    from sqlalchemy import select

    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageRun
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import queue_library_reprocess

    with session_scope() as session:
        _seed(session)

    result = queue_library_reprocess(mode="derived_only")
    assert result["queued"] == 1  # only the recording with a transcript
    assert result["skipped"] == 1  # the downloaded-no-transcript one
    with session_scope() as session:
        assert session.get(PlaudFile, "done1").status == FileStatus.partial
        runs = list(
            session.scalars(select(StageRun).where(StageRun.file_id == "done1"))
        )
        assert {run.stage for run in runs} == {
            StageName.summarize,
            StageName.mind_map,
            StageName.index,
        }
        assert all(run.detail.get("derived_only") is True for run in runs)
        assert all(run.detail.get("stale") is True for run in runs)


def test_force_resets_all_stages_to_pending(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    from sqlalchemy import select

    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import queue_library_reprocess

    with session_scope() as session:
        _seed(session)

    queue_library_reprocess(mode="force")
    with session_scope() as session:
        assert session.get(PlaudFile, "done1").status == FileStatus.downloaded
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "done1", StageRun.stage == StageName.summarize
            )
        )
        assert run.status == StageStatus.pending


def test_dry_run_changes_nothing(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import queue_library_reprocess

    with session_scope() as session:
        _seed(session)

    result = queue_library_reprocess(mode="resume", dry_run=True)
    assert result["queued"] == 2
    with session_scope() as session:
        assert session.get(PlaudFile, "done1").status == FileStatus.done
        assert session.get(PlaudFile, "dl1").status == FileStatus.downloaded


def test_limit_and_status_filter(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import queue_library_reprocess

    with session_scope() as session:
        _seed(session)

    assert queue_library_reprocess(mode="resume", limit=1, dry_run=True)["queued"] == 1
    only_done = queue_library_reprocess(mode="resume", statuses=["done"], dry_run=True)
    assert only_done["queued"] == 1


def test_unknown_mode_rejected(monkeypatch, tmp_path):
    _init_db(monkeypatch, tmp_path)
    import pytest

    from localplaud.worker.pipeline import queue_library_reprocess

    with pytest.raises(ValueError):
        queue_library_reprocess(mode="nope")
