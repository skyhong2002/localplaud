"""Stage attempts form an append-only, catalog-priced usage ledger."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select, text


def _reset(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'usage.db'}")
    monkeypatch.setenv("LOCALPLAUD_ASR__PROVIDER", "faster-whisper")
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


def test_process_peak_memory_normalizes_macos_and_linux_units(monkeypatch):
    import resource

    import localplaud.providers.usage as usage_module

    monkeypatch.setattr(
        resource,
        "getrusage",
        lambda _scope: SimpleNamespace(ru_maxrss=100 * 1024 * 1024),
    )
    monkeypatch.setattr(usage_module.sys, "platform", "darwin")
    assert usage_module.process_peak_memory_mb() == 100.0

    monkeypatch.setattr(
        resource,
        "getrusage",
        lambda _scope: SimpleNamespace(ru_maxrss=100 * 1024),
    )
    monkeypatch.setattr(usage_module.sys, "platform", "linux")
    assert usage_module.process_peak_memory_mb() == 100.0


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
    monkeypatch.setattr(pipeline, "process_peak_memory_mb", lambda: 256.5)
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
        assert attempt.usage["process_peak_memory_mb"] == 256.5
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


def test_stage_attempt_migration_rebuilds_legacy_deployed_schema(tmp_path):
    from localplaud.db.migrations import migrate_stage_attempt_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO plaud_files (id) VALUES ('recording')"))
        connection.execute(text("""
            CREATE TABLE stage_attempts (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                file_id VARCHAR(64) NOT NULL,
                stage VARCHAR(32) NOT NULL,
                attempt INTEGER NOT NULL,
                status VARCHAR(20) NOT NULL,
                provider VARCHAR(64), model VARCHAR(128),
                artifact_source VARCHAR(32),
                profile_snapshot JSON NOT NULL,
                detail JSON NOT NULL,
                error TEXT, started_at DATETIME NOT NULL,
                completed_at DATETIME, latency_ms BIGINT,
                usage JSON NOT NULL, estimated_cost FLOAT, actual_cost FLOAT
            )
        """))
        connection.execute(text("""
            INSERT INTO stage_attempts (
                file_id, stage, attempt, status, profile_snapshot, detail,
                started_at, usage, estimated_cost
            ) VALUES (
                'recording', 'transcribe', 1, 'completed', '{"version": 1}', '{}',
                CURRENT_TIMESTAMP, '{"audio_seconds": 12}', 0.25
            )
        """))

    assert migrate_stage_attempt_schema(engine) == ["stage_attempts"]
    assert migrate_stage_attempt_schema(engine) == []
    columns = {column["name"] for column in inspect(engine).get_columns("stage_attempts")}
    assert "resolved_profile_snapshot" in columns
    assert "profile_snapshot" not in columns
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT resolved_profile_snapshot, usage, estimated_cost_usd
            FROM stage_attempts
        """)).one()
    assert '"version": 1' in row.resolved_profile_snapshot
    assert '"audio_seconds": 12' in row.usage
    assert row.estimated_cost_usd == 0.25


def test_external_cost_ceiling_requires_pricing_and_reserves_budget(tmp_path):
    from sqlalchemy.orm import Session

    from localplaud.db.models import (
        Base,
        ModelCatalogEntry,
        PlaudFile,
        ProviderConnection,
        StageAttempt,
        StageName,
        StageStatus,
    )
    from localplaud.providers.usage import CostPolicyError, enforce_cost_ceiling

    engine = create_engine(f"sqlite:///{tmp_path / 'policy.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(PlaudFile(id="budget", filename="Budget"))
        connection = ProviderConnection(
            key="asr:priced-cloud",
            name="Priced cloud",
            provider_type="openai",
            execution_target="cloud",
            data_egress=True,
        )
        session.add(connection)
        session.flush()
        model = ModelCatalogEntry(
            connection_id=connection.id,
            model_key="audio-model",
            display_name="Audio model",
            capabilities={"metadata": {}},
        )
        session.add(model)
        session.add(
            StageAttempt(
                file_id="budget",
                stage=StageName.summarize,
                attempt=1,
                status=StageStatus.completed,
                estimated_cost_usd=0.005,
                usage={},
            )
        )
        session.commit()
        snapshot = {
            "policy": {"cost_ceiling": 0.02},
            "stages": {
                "transcribe": {
                    "connection": connection.key,
                    "model": model.model_key,
                    "execution_target": "cloud",
                }
            },
        }
        with pytest.raises(CostPolicyError, match="cost is unknown"):
            enforce_cost_ceiling(session, "budget", "transcribe", snapshot, {"audio_seconds": 60})

        snapshot["stages"]["transcribe"]["execution_target"] = "local"
        local = enforce_cost_ceiling(
            session, "budget", "transcribe", snapshot, {"audio_seconds": 60}
        )
        assert local["projected_usd"] == 0
        snapshot["stages"]["transcribe"]["execution_target"] = "cloud"
        model.capabilities = {"metadata": {"pricing": {"free": True}}}
        session.flush()
        free = enforce_cost_ceiling(
            session, "budget", "transcribe", snapshot, {"audio_seconds": 60}
        )
        assert free["projected_usd"] == 0

        model.capabilities = {"metadata": {"pricing": {"audio_per_minute_usd": 0.01}}}
        session.flush()
        allowed = enforce_cost_ceiling(
            session, "budget", "transcribe", snapshot, {"audio_seconds": 60}
        )
        assert allowed["spent_usd"] == pytest.approx(0.005)
        assert allowed["projected_usd"] == pytest.approx(0.01)
        assert allowed["after_projection_usd"] == pytest.approx(0.015)
        snapshot["policy"]["cost_ceiling"] = 0.014
        with pytest.raises(CostPolicyError, match="would exceed"):
            enforce_cost_ceiling(session, "budget", "transcribe", snapshot, {"audio_seconds": 60})


def test_pipeline_blocks_provider_call_then_resumes_under_new_ceiling(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    from localplaud.asr.base import Segment, Transcript
    from localplaud.db.models import (
        ExecutionProfile,
        FileStatus,
        ModelCatalogEntry,
        PlaudFile,
        ProviderConnection,
        StageAttempt,
    )
    from localplaud.db.session import session_scope
    from localplaud.providers.usage import CostPolicyError

    audio = tmp_path / "ceiling.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="ceiling",
                filename="Ceiling",
                status=FileStatus.downloaded,
                audio_path=str(audio),
                duration_ms=60_000,
            )
        )
        profile = session.scalar(select(ExecutionProfile).where(ExecutionProfile.is_system_default))
        profile.no_egress = False
        profile.privacy_policy = "allow-egress"
        profile.cost_ceiling = 0.01
        connection = session.scalar(
            select(ProviderConnection).where(ProviderConnection.key == "asr:faster-whisper")
        )
        connection.execution_target = "cloud"
        connection.data_egress = True
        model = session.scalar(
            select(ModelCatalogEntry).where(
                ModelCatalogEntry.connection_id == connection.id,
                ModelCatalogEntry.model_key == settings.asr.faster_whisper.model,
            )
        )
        capability = dict(model.capabilities)
        capability["execution_target"] = "cloud"
        capability["data_egress"] = True
        capability["metadata"] = {"pricing": {"audio_per_minute_usd": 0.02}}
        model.capabilities = capability

    calls = 0

    def fake_asr(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return Transcript(
            segments=[Segment(text="allowed", start=0, end=60)],
            duration=60,
            language="en",
            provider="faster-whisper",
            model=settings.asr.faster_whisper.model,
        )

    monkeypatch.setattr(pipeline.transcribe, "run_asr", fake_asr)
    with pytest.raises(CostPolicyError, match="would exceed"):
        pipeline.process_file("ceiling", settings)
    assert calls == 0
    from fastapi.testclient import TestClient

    from localplaud.api.app import app

    client = TestClient(app)
    blocked_page = client.get("/file/ceiling")
    assert blocked_page.status_code == 200
    assert "Cost boundary" in blocked_page.text
    assert "would exceed" in blocked_page.text
    budget = client.get("/api/files/ceiling/usage").json()["budget"]
    assert budget["ceiling_usd"] == pytest.approx(0.01)
    assert budget["remaining_usd"] == pytest.approx(0.01)
    with session_scope() as session:
        attempts = list(
            session.scalars(select(StageAttempt).where(StageAttempt.file_id == "ceiling"))
        )
        assert len(attempts) == 1 and attempts[0].status == "failed"
        assert "would exceed" in attempts[0].error
        session.scalar(
            select(ExecutionProfile).where(ExecutionProfile.is_system_default)
        ).cost_ceiling = 0.03
    pipeline.process_file("ceiling", settings)
    assert calls == 1
    with session_scope() as session:
        attempts = list(
            session.scalars(
                select(StageAttempt)
                .where(StageAttempt.file_id == "ceiling")
                .order_by(StageAttempt.attempt)
            )
        )
        assert [item.status.value for item in attempts] == ["failed", "completed"]
        assert attempts[1].estimated_cost_usd == pytest.approx(0.02)
