"""Non-secret runtime identity and support diagnostics."""

from __future__ import annotations

import os
import platform
import re
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db.models import (
    AutomationRule,
    BrowserSession,
    EmailIntegration,
    PlaudFile,
    RemoteWorker,
    StageRun,
    WebhookIntegration,
)
from .db.session import get_engine

_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")


def package_version() -> str:
    try:
        return version("localplaud")
    except PackageNotFoundError:
        return "development"


def build_commit() -> str | None:
    value = os.environ.get("LOCALPLAUD_BUILD_COMMIT", "").strip().lower()
    return value if _COMMIT_RE.fullmatch(value) else None


def access_boundary(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    browser_login = bool(settings.api.login_password and settings.api.session_secret)
    active_sessions = 0
    if browser_login:
        with Session(get_engine()) as session:
            active_sessions = session.scalar(
                select(func.count()).select_from(BrowserSession).where(
                    BrowserSession.expires_at > datetime.now(UTC)
                )
            ) or 0
    return {
        "application_token_configured": bool(settings.api.auth_token),
        "browser_login_configured": browser_login,
        "reverse_proxy": "external / not observable by localplaud",
        "active_sessions": active_sessions if browser_login else None,
        "session_detail": (
            "localplaud stores only peppered session-token hashes; active sessions can be "
            "reviewed and revoked from Settings"
            if browser_login
            else "localplaud Web App login is not configured"
        ),
    }


def about_info(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    return {
        "product": "localplaud",
        "version": package_version(),
        "build_commit": build_commit(),
        "license": "MIT",
        "python": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        "support": {
            "documentation": "/api/docs",
            "status": "/status",
            "repository": "https://github.com/skyhong2002/localplaud",
        },
        "access": access_boundary(settings),
    }


def _enum_counts(session: Session, column) -> dict[str, int]:
    return {
        (key.value if hasattr(key, "value") else str(key)): int(count)
        for key, count in session.execute(select(column, func.count()).group_by(column))
    }


def safe_diagnostics(session: Session, settings: Settings | None = None) -> dict:
    """Return aggregate diagnostics with no paths, names, content, URLs, or secrets."""
    settings = settings or get_settings()
    database = make_url(settings.store.database_url)
    return {
        "schema": "localplaud-safe-diagnostics/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime": about_info(settings),
        "deployment": {
            "database": database.get_backend_name(),
            "artifact_mode": settings.pipeline.artifact_mode,
            "poller_enabled": settings.poller.enabled,
            "metadata_first": not settings.poller.auto_download,
            "enabled_stages": {
                "convert": settings.pipeline.convert,
                "transcribe": settings.pipeline.transcribe,
                "diarize": settings.pipeline.diarize,
                "summarize": settings.pipeline.summarize,
                "mind_map": settings.pipeline.mind_map,
                "index": settings.pipeline.index,
            },
            "providers": {
                "plaud": settings.plaud.provider,
                "asr": settings.asr.provider,
                "diarization": settings.diarize.provider,
                "llm": settings.llm.provider,
                "embeddings": settings.embeddings.provider,
            },
        },
        "counts": {
            "recordings": int(session.scalar(select(func.count()).select_from(PlaudFile)) or 0),
            "recording_status": _enum_counts(session, PlaudFile.status),
            "stage_status": _enum_counts(session, StageRun.status),
            "automation_rules": int(
                session.scalar(select(func.count()).select_from(AutomationRule)) or 0
            ),
            "webhook_integrations": int(
                session.scalar(select(func.count()).select_from(WebhookIntegration)) or 0
            ),
            "email_integrations": int(
                session.scalar(select(func.count()).select_from(EmailIntegration)) or 0
            ),
            "remote_workers": int(
                session.scalar(select(func.count()).select_from(RemoteWorker)) or 0
            ),
        },
        "redaction": {
            "excluded": [
                "recording ids, titles, transcripts, notes, and audio",
                "database, media, token, and configuration paths",
                "public, provider, worker, webhook, and email URLs or addresses",
                "errors, request/response payloads, environment variables, and secret values",
            ]
        },
    }
