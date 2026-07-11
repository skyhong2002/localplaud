"""Stage attempts form an append-only, catalog-priced usage ledger."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select, text


def _reset(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'usage.db'}")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONVERT", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__DIARIZE", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__SUMMARIZE", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__MIND_MAP", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__INDEX", "false")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    settings = get_settings(reload=True)
    from localplaud.db.session import init_db

    init_db()
    return settings


def test_usage_normalization_and_catalog_pricing():
    from localplaud.providers.usage import estimate_cost, normalize_usage

    usage = normalize_usage({"input_chars": 4000, "output_chars": 2000, "audio_seconds": 120})
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 500
    assert usage["tokens_estimated"] is True
    cost = estimate_cost(
        usage,
        {
            "input_per_million_tokens_usd": 10,
            "output_per_million_tokens_usd": 20,
            "audio_per_minute_usd": 0.006,
            "per_request_usd": 0.001,
        },
    )
    assert cost == pytest.approx(0.033)


def test_pipeline_persists_priced_attempt_and_usage_api(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient

    import localplaud.worker.pipeline as pipeline
    from localplaud.asr.base import Segment, Transcript
    from localplaud.db.models import (
        FileStatus,
        ModelCatalogEntry,
        PlaudFile,
        ProviderConnection,
        StageAttempt,
    )
    from localplaud.db.session import session_scope

    audio = tmp_path / "priced.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="priced",
                filename="Priced recording",
                status=FileStatus.downloaded,
                audio_path=str(audio),
                duration_ms=60_000,
            )
        )
        connection = session.scalar(
            select(ProviderConnection).where(ProviderConnection.key == "asr:faster-whisper")
        )
        model = session.scalar(
            select(ModelCatalogEntry).where(
                ModelCatalogEntry.connection_id == connection.id,
                ModelCatalogEntry.model_key == settings.asr.faster_whisper.model,
            )
        )
        model.capabilities = dict(model.capabilities) | {
            "metadata": {"pricing": {"audio_per_minute_usd": 0.006}}
        }
    monkeypatch.setattr(
        pipeline.transcribe,
        "run_asr",
        lambda *_args, **_kwargs: Transcript(
            segments=[Segment(text="hello measured world", start=0, end=60)],
            language="en",
            duration=60,
            provider="faster-whisper",
            model=settings.asr.faster_whisper.model,
        ),
    )
    pipeline.process_file("priced", settings)
    with session_scope() as session:
        attempt = session.scalar(
            select(StageAttempt).where(
                StageAttempt.file_id == "priced", StageAttempt.stage == "transcribe"
            )
        )
        assert attempt.status == "completed"
        assert attempt.usage["audio_seconds"] == 60
        assert attempt.usage["output_tokens"] > 0
        assert attempt.estimated_cost_usd == pytest.approx(0.006)
        assert attempt.latency_ms is not None and attempt.latency_ms >= 0
        assert attempt.resolved_profile_snapshot["stages"]["transcribe"]["model"]

    from localplaud.api.app import app

    client = TestClient(app)
    response = client.get("/api/files/priced/usage")
    assert response.status_code == 200
    assert response.json()["totals"]["estimated_cost_usd"] == pytest.approx(0.006)
    page = client.get("/file/priced")
    assert "Attempt usage ledger" in page.text
    assert "60.0 audio s" in page.text


def test_failed_then_successful_stage_keeps_both_attempts(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile, StageAttempt, StageName
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _begin_stage, _fail_stage, _finish_stage

    with session_scope() as session:
        session.add(PlaudFile(id="history", filename="History"))
    _begin_stage("history", StageName.transcribe)
    _fail_stage("history", StageName.transcribe, RuntimeError("temporary"))
    _begin_stage("history", StageName.transcribe)
    _finish_stage(
        "history",
        StageName.transcribe,
        provider="fake",
        model="model",
        usage={"audio_seconds": 12, "output_chars": 40},
    )
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(StageAttempt)
                .where(StageAttempt.file_id == "history")
                .order_by(StageAttempt.attempt)
            )
        )
        assert [(row.attempt, row.status.value) for row in rows] == [
            (1, "failed"),
            (2, "completed"),
        ]
        assert rows[0].error == "temporary"
        assert rows[1].usage["audio_seconds"] == 12


def test_stage_attempt_migration_is_idempotent(tmp_path):
    from localplaud.db.migrations import migrate_stage_attempt_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)"))
    assert migrate_stage_attempt_schema(engine) == ["stage_attempts"]
    assert migrate_stage_attempt_schema(engine) == []
