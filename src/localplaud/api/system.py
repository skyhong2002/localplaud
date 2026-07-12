"""About, access-boundary, and redacted support diagnostics API."""

import json

from fastapi import APIRouter
from fastapi.responses import Response

from ..db.session import session_scope
from ..system_info import about_info, safe_diagnostics

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/about")
def about() -> dict:
    return about_info()


@router.get("/diagnostics.json")
def diagnostics_download() -> Response:
    with session_scope() as session:
        payload = safe_diagnostics(session)
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="localplaud-diagnostics.json"',
            "Cache-Control": "no-store",
        },
    )
