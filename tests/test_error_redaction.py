from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import localplaud.db.session  # noqa: F401 - registers the persistence boundary
from localplaud.db.migrations import redact_legacy_error_text
from localplaud.db.models import (
    Base,
    PlaudFile,
    ProviderConnection,
    StageAttempt,
    StageName,
    StageRun,
    StageStatus,
)
from localplaud.error_redaction import REDACTED, sanitize_error


@pytest.mark.parametrize(
    ("raw", "expected_context"),
    [
        (
            "OpenAI 401 Incorrect API key: sk-mSRJr************************CyfO",
            "OpenAI 401",
        ),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.private", "Authorization"),
        ("proxy rejected Basic dXNlcjpwYXNz", "proxy rejected Basic"),
        ('provider failed: api_key="plain-secret-value" status=403', "status=403"),
        ("connect https://alice:hunter2@example.test/v1 timed out", "timed out"),
    ],
)
def test_sanitize_error_redacts_credentials_and_preserves_context(raw, expected_context):
    sanitized = sanitize_error(raw)

    assert REDACTED in sanitized
    assert expected_context in sanitized
    for fragment in ("mSRJr", "CyfO", "eyJhbGci", "dXNlcj", "plain-secret", "hunter2"):
        assert fragment not in sanitized


def test_sanitize_error_preserves_ordinary_runtime_detail_and_truncates():
    detail = "CUDA out of memory while loading large-v3-turbo on device 0"
    assert sanitize_error(detail) == detail
    assert sanitize_error("provider unavailable after timeout", max_length=12) == "provider ..."


def test_orm_persistence_redacts_recording_stage_attempt_and_diagnostics(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'redaction.db'}")
    Base.metadata.create_all(engine)
    masked_key = "sk-mSRJr************************CyfO"
    with Session(engine) as session:
        session.add(PlaudFile(id="redacted", filename="recording", error=f"OpenAI 401 {masked_key}"))
        session.add(
            StageRun(
                file_id="redacted",
                stage=StageName.transcribe,
                status=StageStatus.failed,
                attempts=1,
                error="Authorization: Bearer stage-token-value",
                detail={"fallback_failures": [{"error": "password=stage-password"}]},
            )
        )
        session.add(
            StageAttempt(
                file_id="redacted",
                stage=StageName.transcribe,
                attempt=1,
                status=StageStatus.failed,
                error="POST https://worker:worker-pass@example.test failed",
            )
        )
        session.add(
            ProviderConnection(
                key="redaction-provider",
                name="Redaction provider",
                provider_type="openai",
                health={"status": "unavailable", "detail": "api_key=health-secret HTTP 401"},
            )
        )
        session.commit()

    with Session(engine) as session:
        recording = session.get(PlaudFile, "redacted")
        run = session.query(StageRun).one()
        attempt = session.query(StageAttempt).one()
        provider = session.query(ProviderConnection).one()
        persisted = repr([recording.error, run.error, run.detail, attempt.error, provider.health])

    assert persisted.count(REDACTED) == 5
    assert "OpenAI 401" in persisted and "HTTP 401" in persisted and "failed" in persisted
    for fragment in ("mSRJr", "CyfO", "stage-token", "stage-password", "worker-pass", "health-secret"):
        assert fragment not in persisted


def test_legacy_core_error_redaction_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-errors.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(PlaudFile(id="legacy", filename="x"))
        session.add(
            StageRun(
                file_id="legacy",
                stage=StageName.transcribe,
                status=StageStatus.failed,
                attempts=1,
            )
        )
        session.add(
            StageAttempt(
                file_id="legacy",
                stage=StageName.transcribe,
                attempt=1,
                status=StageStatus.failed,
            )
        )
        session.add(
            ProviderConnection(
                key="legacy-provider",
                name="Legacy provider",
                provider_type="openai",
            )
        )
        session.commit()
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE plaud_files SET error = :error WHERE id = 'legacy'"),
            {"error": "OpenAI 401 sk-old************************Tail"},
        )
        connection.execute(
            text(
                "UPDATE stage_runs SET error = :error, detail = :detail "
                "WHERE file_id = 'legacy' AND stage = 'transcribe'"
            ),
            {
                "error": "Authorization: Bearer legacy-stage-token",
                "detail": '{"reason":"password=legacy-detail-password"}',
            },
        )
        connection.execute(
            text(
                "UPDATE stage_attempts SET error = :error "
                "WHERE file_id = 'legacy' AND stage = 'transcribe' AND attempt = 1"
            ),
            {"error": "password=legacy-attempt-password"},
        )
        connection.execute(
            text("UPDATE provider_connections SET health = :health WHERE key = 'legacy-provider'"),
            {
                "health": (
                    '{"status":"unavailable",'
                    '"detail":"api_key=legacy-health-secret HTTP 401"}'
                )
            },
        )

    assert redact_legacy_error_text(engine) == 5
    assert redact_legacy_error_text(engine) == 0
    with engine.connect() as connection:
        persisted = repr(
            [
                connection.execute(text(f"SELECT {column} FROM {table}")).scalar_one()
                for table, column in (
                    ("plaud_files", "error"),
                    ("stage_runs", "error"),
                    ("stage_runs", "detail"),
                    ("stage_attempts", "error"),
                    ("provider_connections", "health"),
                )
            ]
        )
    with Session(engine) as session:
        provider_health = session.query(ProviderConnection).one().health

    assert persisted.count(REDACTED) == 5
    assert provider_health == {"status": "unavailable", "detail": "api_key=[REDACTED] HTTP 401"}
    for fragment in (
        "old",
        "Tail",
        "legacy-stage-token",
        "legacy-detail-password",
        "legacy-attempt-password",
        "legacy-health-secret",
    ):
        assert fragment not in persisted
