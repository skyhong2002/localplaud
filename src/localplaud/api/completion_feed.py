"""Narrow machine credential for completion notifications, independent of Web login."""

import hashlib
import hmac

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from ..config import get_settings
from ..db.models import PlaudFile
from ..db.session import session_scope

PATH = "/api/integrations/completion-status"
router = APIRouter()


@router.get(PATH)
def completion_status(request: Request):
    expected = get_settings().api.completion_token_sha256
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if (
        not expected
        or scheme.lower() != "bearer"
        or not 32 <= len(token) <= 256
        or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), expected)
    ):
        raise HTTPException(401, "Invalid completion notification credential")
    # Only metadata used in notifications. Never fetch transcripts, notes, audio
    # URLs, provider settings or account data, even when those artifacts exist.
    with session_scope() as db:
        rows = db.execute(
            select(
                PlaudFile.id,
                PlaudFile.status,
                PlaudFile.local_title,
                PlaudFile.generated_title,
                PlaudFile.filename,
                PlaudFile.start_time_ms,
                PlaudFile.duration_ms,
            )
            .where(PlaudFile.is_trash.is_(False))
            .order_by(PlaudFile.id)
        ).all()
    return {
        "files": [
            {
                "id": row.id,
                "status": row.status.value,
                "filename": row.local_title or row.generated_title or row.filename or row.id[:12],
                "start_time_ms": row.start_time_ms,
                "duration_ms": row.duration_ms,
            }
            for row in rows
        ]
    }
