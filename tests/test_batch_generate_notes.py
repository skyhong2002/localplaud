"""Library batch "Generate notes" (desktop bulk bar + phone selection card).

The batch endpoint previews eligibility without side effects, then — only after
explicit confirmation — durably queues note-only regeneration for eligible
recordings. It never reruns ASR, never starts work in the request thread, and
never contacts Plaud. Plaud transcripts never make a recording eligible.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from tests.assets import with_assets

DERIVED = ("summarize", "mind_map", "outline", "index")


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'batch.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    _forbid_work(monkeypatch)
    return TestClient(app)


def _forbid_work(monkeypatch):
    """Queueing must not run pipeline work or reach Plaud from the request."""
    import localplaud.worker.pipeline as pipeline

    def boom(*_args, **_kwargs):
        raise AssertionError("batch Generate notes must only queue durable work")

    for name in ("process_file", "process_derived_artifacts", "claim_processing_work"):
        monkeypatch.setattr(pipeline, name, boom)
    from localplaud.plaud.connections import WorkspacePlaudClient
    from localplaud.plaud.mcp import PlaudMcpClient
    from localplaud.plaud.official import PlaudOfficialClient

    for cls in (WorkspacePlaudClient, PlaudMcpClient, PlaudOfficialClient):
        monkeypatch.setattr(cls, "__init__", boom)


def _seed():
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
    )
    from localplaud.db.session import session_scope

    done = datetime.now(UTC) - timedelta(days=30)

    def local(text="hello"):
        return [
            Transcript(
                provider="faster-whisper",
                language="en",
                source="local",
                text=text,
                segments=[{"text": text, "start": 0.0, "end": 1.0}],
            )
        ]

    def speech():
        return [
            StageRun(stage=stage, status=StageStatus.completed, completed_at=done, attempts=1)
            for stage in (
                StageName.convert,
                StageName.transcribe,
                StageName.align,
                StageName.diarize,
            )
        ]

    with session_scope() as s:
        s.add(
            PlaudFile(
                id="ready",
                filename="Ready local",
                status=FileStatus.done,
                start_time_ms=6000,
                transcripts=local(),
                stage_runs=speech()
                + [
                    StageRun(
                        stage=StageName.summarize,
                        status=StageStatus.completed,
                        completed_at=done,
                        attempts=1,
                    ),
                    StageRun(
                        stage=StageName.mind_map,
                        status=StageStatus.completed,
                        completed_at=done,
                        attempts=1,
                        detail={"mind_map_only": True},
                    ),
                ],
            )
        )
        # A transcript without any derived StageRun rows yet (and no audio).
        s.add(
            PlaudFile(
                id="fresh",
                filename="Fresh local",
                status=FileStatus.error,
                error="notes failed",
                pipeline_retry_count=3,
                start_time_ms=5000,
                transcripts=local(),
                stage_runs=speech(),
            )
        )
        s.add(
            PlaudFile(
                id="trashed",
                filename="Trashed local",
                status=FileStatus.done,
                start_time_ms=4000,
                is_trash=True,
                transcripts=local(),
            )
        )
        s.add(
            PlaudFile(
                id="cloudonly",
                filename="Cloud only",
                status=FileStatus.done,
                start_time_ms=3000,
                transcripts=[
                    Transcript(
                        provider="plaud",
                        source="cloud",
                        text="x",
                        segments=[{"text": "x", "start": 0.0, "end": 1.0}],
                    )
                ],
            )
        )
        s.add(
            PlaudFile(
                id="busy",
                filename="Busy notes",
                status=FileStatus.processing,
                start_time_ms=2000,
                transcripts=local(),
                processing_token="t",
                processing_lease_until=datetime.now(UTC) + timedelta(hours=1),
                stage_runs=[StageRun(stage=StageName.summarize, status=StageStatus.running)],
            )
        )
        s.add(
            PlaudFile(
                id="noaudio",
                filename="No transcript",
                status=FileStatus.downloaded,
                start_time_ms=1000,
            )
        )


def _snapshot():
    from sqlalchemy import select

    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        return {
            row.id: (
                row.status,
                row.error,
                row.pipeline_retry_count,
                sorted(
                    (run.stage.value, run.status.value, repr(run.detail)) for run in row.stage_runs
                ),
                sorted((t.source, t.text) for t in row.transcripts),
            )
            for row in s.scalars(select(PlaudFile))
        }


ALL = ["ready", "fresh", "trashed", "cloudonly", "busy", "noaudio"]


def test_preview_classifies_without_side_effects(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    before = _snapshot()
    response = c.post("/api/files/generate-notes", json={"file_ids": ALL, "dry_run": True})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["dry_run"] is True and data["requested"] == 6 and data["max_files"] == 50
    assert data["eligible"] == ["ready", "fresh"]
    assert data["already_queued"] == []
    reasons = {item["file_id"]: item["reason"] for item in data["skipped"]}
    assert reasons == {
        "trashed": "trash",
        "cloudonly": "no_local_transcript",
        "busy": "processing",
        "noaudio": "no_local_transcript",
    }
    assert all(item["message"] and item["title"] for item in data["skipped"])
    assert _snapshot() == before


def test_confirm_queues_note_only_regeneration_durably(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    before = _snapshot()
    response = c.post("/api/files/generate-notes", json={"file_ids": ALL})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["queued"] == ["ready", "fresh"]
    assert {item["file_id"] for item in data["skipped"]} == {
        "trashed",
        "cloudonly",
        "busy",
        "noaudio",
    }

    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _pending_scope

    after = _snapshot()
    for untouched in ("trashed", "cloudonly", "busy", "noaudio"):
        assert after[untouched] == before[untouched], untouched
    with session_scope() as s:
        for file_id in ("ready", "fresh"):
            row = s.get(PlaudFile, file_id)
            assert row.status == FileStatus.partial and row.error is None
            assert row.pipeline_retry_count == 0
            runs = {run.stage.value: run for run in row.stage_runs}
            # Speech stages and transcripts are never reset: no ASR rerun.
            for stage in ("convert", "transcribe", "align", "diarize"):
                assert runs[stage].status == StageStatus.completed
            for stage in DERIVED:
                assert runs[stage].status == StageStatus.pending
                assert runs[stage].detail["derived_only"] is True
                assert runs[stage].detail["stale"] is True
                assert runs[stage].detail["reason"] == "library batch generate notes"
            assert "mind_map_only" not in runs["mind_map"].detail
            assert [t.text for t in row.transcripts] == ["hello"]
            # The daemon will admit it as note-only (derived) work — even without audio.
            assert _pending_scope(row, get_settings(), datetime.now(UTC)) == "derived"
        assert after["ready"][4] == before["ready"][4]


def test_repeat_request_reports_already_queued_without_reinvalidating(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    assert c.post("/api/files/generate-notes", json={"file_ids": ["ready"]}).status_code == 200
    first = _snapshot()["ready"]
    again = c.post("/api/files/generate-notes", json={"file_ids": ["ready"]})
    assert again.status_code == 200
    assert again.json()["queued"] == [] and again.json()["already_queued"] == ["ready"]
    assert _snapshot()["ready"] == first


def test_rejections_are_atomic_and_actionable(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    before = _snapshot()
    missing = c.post("/api/files/generate-notes", json={"file_ids": ["ready", "nope"]})
    assert missing.status_code == 404
    too_many = c.post("/api/files/generate-notes", json={"file_ids": [f"f{i}" for i in range(51)]})
    assert too_many.status_code == 422
    assert c.post("/api/files/generate-notes", json={"file_ids": []}).status_code == 422
    none = c.post("/api/files/generate-notes", json={"file_ids": ["trashed", "cloudonly", "busy"]})
    assert none.status_code == 409
    assert none.json()["detail"] == "No selected recording can generate notes right now."
    assert _snapshot() == before


def test_untranscribed_only_mode_refuses_to_strand_old_transcripts(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.config import get_settings

    monkeypatch.setattr(get_settings().pipeline, "auto_process_untranscribed_only", True)
    data = c.post("/api/files/generate-notes", json={"file_ids": ["ready"], "dry_run": True}).json()
    assert data["eligible"] == []
    assert data["skipped"][0]["reason"] == "automatic_restricted"


def test_library_offers_batch_generate_with_confirmation(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    page = c.get("/")
    html = page.text
    bar = re.search(
        r'<div class="bulkbar batch-bar" id="bulkbar">.*?<div class="bulkmsg"', html, re.S
    ).group(0)
    # Phone selection card action and desktop bulk-bar button share the dialog.
    assert re.search(
        r'<button class="batch-action" type="button" data-batch-action="generate" '
        r'aria-disabled="true" aria-haspopup="dialog" aria-controls="bulk-generate-dialog">',
        bar,
    )
    assert re.search(
        r'<button class="btn sec bulk-generate" type="button" id="bulk-generate-open" '
        r'data-batch-action="generate" aria-disabled="true"',
        bar,
    )
    dialog = re.search(r'<dialog[^>]*id="bulk-generate-dialog".*?</dialog>', html, re.S).group(0)
    assert 'id="bulk-generate-confirm" disabled' in dialog
    assert "Speech recognition is not rerun" in dialog
    # Preview first (dry run), confirm sends only the previewed eligible ids.
    assert "generateFetch(fileIds, true)" in html
    assert "generateFetch(generatePreview.eligible, false)" in html
    assert "generate: generateReason(ids)," in html
    assert ".dataset.canGenerate === '1'" in html
    shell = with_assets(c, page)
    assert ".batch-bar > .bulk-generate" in shell
    # Trash cannot generate notes, so the batch action is not offered there.
    trash = c.get("/?view=trash").text
    assert 'data-batch-action="generate"' not in trash
    assert 'id="bulk-generate-dialog"' not in trash


def test_batch_generate_strings_are_translated():
    from localplaud.i18n import CATALOGS
    from localplaud.i18n_ext.batch_notes import ZH_HANT_TW

    catalog = CATALOGS["zh-Hant-TW"]
    for key in ZH_HANT_TW:
        assert catalog[key] == ZH_HANT_TW[key], key


from .test_accounts import client  # noqa: E402,F401
from .test_workspace_http import two_workspaces  # noqa: E402,F401


def test_batch_generate_cannot_reach_another_workspace(two_workspaces):  # noqa: F811
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.db.tenancy import system_scope

    from .test_accounts import login

    web, theirs = two_workspaces
    login(web, "sky")
    for dry_run in (True, False):
        response = web.post(
            "/api/files/generate-notes", json={"file_ids": [theirs], "dry_run": dry_run}
        )
        assert response.status_code == 404
        mixed = web.post(
            "/api/files/generate-notes", json={"file_ids": ["mine", theirs], "dry_run": dry_run}
        )
        assert mixed.status_code == 404
    with system_scope(), session_scope() as db:
        assert db.get(PlaudFile, theirs).stage_runs == []
        assert db.get(PlaudFile, "mine").stage_runs == []
    # The owner's own recording is eligible through the same scoped endpoint.
    own = web.post("/api/files/generate-notes", json={"file_ids": ["mine"], "dry_run": True}).json()
    assert own["eligible"] == ["mine"]
