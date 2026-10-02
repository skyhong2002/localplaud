"""The synthetic demo library seeds through the real models and renders."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from sqlalchemy import func, select

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "seed_demo.py"


def _load_seed():
    spec = importlib.util.spec_from_file_location("seed_demo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_seed_demo_builds_a_complete_synthetic_library(monkeypatch, tmp_path):
    import localplaud.config as config
    import localplaud.db.session as db_session

    out = tmp_path / "demo"
    # build() points settings at the demo; register the variables so they are restored.
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", "sqlite://")
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    monkeypatch.setattr(config, "_settings", None)

    result = _load_seed().build(out, audio=False)

    assert result["recordings"] == 40
    assert result["states"]["done"] >= 20
    for state in ("failed_asr", "degraded_diarize", "failed_index", "trash",
                  "processing_asr", "metadata_only"):
        assert result["states"][state] >= 1

    from localplaud.db.models import (
        AskThread,
        AutomationRule,
        FileStatus,
        Folder,
        PlaudFile,
        StageName,
        StageRun,
        Summary,
        Transcript,
        TranscriptRevision,
    )
    from localplaud.db.session import session_scope

    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(Folder)) == 5
        assert session.scalar(select(func.count()).select_from(AutomationRule)) == 4
        assert session.scalar(select(func.count()).select_from(AskThread)) >= 4
        assert session.scalar(select(func.count()).select_from(TranscriptRevision)) >= 5
        # Every derived artifact is local: the demo never depends on Plaud output.
        assert set(session.scalars(select(Transcript.source).distinct())) == {"local"}
        assert set(session.scalars(select(Summary.source).distinct())) == {"local"}
        assert session.scalar(
            select(func.count()).select_from(Summary).where(Summary.template == "mind_map")
        ) >= 20
        # Embedding failure leaves a usable transcript and notes.
        failed_index = session.scalars(
            select(StageRun.file_id).where(
                StageRun.stage == StageName.index, StageRun.status == "failed"
            )
        ).all()
        assert failed_index
        for file_id in failed_index:
            row = session.get(PlaudFile, file_id)
            assert row.status == FileStatus.partial
            assert row.local_transcript is not None
            assert any(s.template != "mind_map" for s in row.summaries)
        # Diarized transcripts carry word timestamps and speaker ids.
        segment = session.scalars(
            select(Transcript).where(Transcript.has_speakers.is_(True))
        ).first().segments[0]
        assert segment["speaker"] and segment["words"][0]["start"] is not None

    from fastapi.testclient import TestClient

    from localplaud.api.app import app

    client = TestClient(app)
    for path in ("/home", "/?workspace=true", "/?view=trash&workspace=true"):
        assert client.get(path).status_code == 200
    file_id = client.get("/api/files").json()["files"][0]["id"]
    assert client.get(f"/file/{file_id}?workspace=true").status_code == 200
