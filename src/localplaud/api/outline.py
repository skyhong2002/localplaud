"""Chapter outline API: read the live outline, regenerate it, record feedback.

Contract (``GET /api/files/{file_id}/outline``)::

    {
      "file_id": str,
      "status": "missing" | "pending" | "running" | "completed" | "failed" | "skipped",
      "stale": bool,               # built from an older transcript revision
      "error": str | null,         # last outline attempt's error, if it failed
      "method": "llm" | "time_slices" | null,
      "chapters": [{"start_ms": int, "end_ms": int, "title": str}, ...],
      "provenance": {
        "source": "local", "provider": str, "model": str | null,
        "prompt_version": str, "created_at": iso8601,
        "transcript_revision": int, "transcript_id": int | null,
        "transcript_source": str | null, "method": str,
        "revision": int, "language": str | null
      } | null,
      "feedback": "up" | "down" | null,
      "duration_ms": int | null
    }

``chapters`` always holds the live (latest) outline even while a newer one is
pending, running, or has failed, so the UI never loses a usable outline.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import get_settings
from ..db.models import Outline, PlaudFile, StageName, StageStatus
from ..db.session import session_scope
from ..db.tenancy import run_in_current_workspace, scoped_to_file

router = APIRouter()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _current_lineage(row: PlaudFile) -> dict | None:
    from ..worker.pipeline import _select_raw_transcript

    raw = _select_raw_transcript(row, get_settings())
    if raw is None:
        return None
    revision = row.corrected_transcript_for_source(raw.source)
    return {
        "input_transcript_id": raw.id,
        "input_transcript_revision": revision.revision if revision else 0,
        "input_transcript_source": raw.source,
    }


def live_outline(row: PlaudFile) -> Outline | None:
    local = [item for item in row.outlines if item.source == "local"]
    return local[-1] if local else None


def outline_is_stale(row: PlaudFile, live: Outline | None = None) -> bool:
    from ..worker.pipeline import outline_is_current

    live = live if live is not None else live_outline(row)
    if live is None:
        return False
    run = next((item for item in row.stage_runs if item.stage == StageName.outline), None)
    if run is not None and (run.detail or {}).get("stale"):
        return True
    return not outline_is_current(live, _current_lineage(row))


def outline_payload(row: PlaudFile) -> dict:
    live = live_outline(row)
    run = next((item for item in row.stage_runs if item.stage == StageName.outline), None)
    stale = outline_is_stale(row, live)
    if run is None:
        status = "completed" if live is not None else "missing"
    elif run.status == StageStatus.running:
        status = "running"
    elif run.status in {StageStatus.failed, StageStatus.degraded}:
        status = "failed"
    elif run.status == StageStatus.skipped:
        status = "completed" if live is not None else "skipped"
    elif run.status == StageStatus.pending:
        # Stale marking leaves the run pending; that is an out-of-date outline,
        # not queued work, as long as a live outline exists.
        status = (
            "completed"
            if live is not None
            and (run.detail or {}).get("stale")
            and not (run.detail or {}).get("outline_only")
            else "pending"
        )
    else:
        status = "completed" if live is not None else "missing"
    provenance = None
    if live is not None:
        provenance = {
            "source": live.source,
            "provider": live.provider,
            "model": live.model,
            "prompt_version": live.prompt_version,
            "created_at": _iso(live.created_at),
            "transcript_revision": live.input_transcript_revision,
            "transcript_id": live.input_transcript_id,
            "transcript_source": live.input_transcript_source,
            "method": live.method,
            "revision": live.revision,
            "language": live.language,
        }
    return {
        "file_id": row.id,
        "status": status,
        "stale": stale,
        "error": run.error if run is not None and status == "failed" else None,
        "method": live.method if live is not None else None,
        "chapters": [
            {
                "start_ms": int(chapter["start_ms"]),
                "end_ms": int(chapter["end_ms"]),
                "title": str(chapter["title"]),
            }
            for chapter in (live.chapters if live is not None else [])
        ],
        "provenance": provenance,
        "feedback": live.feedback if live is not None else None,
        "duration_ms": row.duration_ms,
    }


@router.get("/api/files/{file_id}/outline")
def get_outline(file_id: str) -> dict:
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise HTTPException(status_code=404, detail="recording not found")
        payload = outline_payload(row)
        from ..providers.fallback import candidate_snapshots
        from ..providers.service import resolve_recording_profile

        settings = get_settings()
        template = row.note_template_key or settings.pipeline.summary_template
        try:
            snapshot = resolve_recording_profile(
                session,
                file_id,
                template_key=settings.pipeline.summary_template if template == "auto" else template,
            ).to_dict()
            choices = []
            for candidate in candidate_snapshots(snapshot, "mind_map"):
                selection = (candidate.get("stages") or {}).get("mind_map") or {}
                choices.append(
                    {
                        key: selection.get(key)
                        for key in ("provider_type", "model", "execution_target", "data_egress")
                    }
                )
            payload["generation"] = {
                "llm_available": bool(choices[0].get("model")),
                "selection": choices[0],
                "fallbacks": choices[1:],
                "cost_ceiling_usd": (snapshot.get("policy") or {}).get("cost_ceiling"),
                "profile_stage": "mind_map",
            }
        except ValueError:
            payload["generation"] = {"llm_available": False, "selection": None, "fallbacks": []}
        return payload


class RegenerateRequest(BaseModel):
    # ``None`` uses the configured pipeline.outline_method. ``time_slices`` is the
    # explicit, model-free alternative and is labelled as such in provenance.
    method: Literal["llm", "time_slices"] | None = None


def _start_worker(target, *args, **kwargs) -> None:
    """Run the rebuild off the request thread (tests replace this)."""
    threading.Thread(
        target=run_in_current_workspace(target), args=args, kwargs=kwargs, daemon=True
    ).start()


@scoped_to_file
def _run_rebuild(file_id: str, claim_token: str, method: str | None) -> None:
    from ..worker.pipeline import process_outline_only

    try:
        process_outline_only(file_id, claim_token=claim_token, method=method)
    except Exception:  # noqa: BLE001 - failure is recorded on the outline stage run
        pass


@router.post("/api/files/{file_id}/outline/regenerate", status_code=202)
def regenerate_outline(file_id: str, body: RegenerateRequest | None = None):
    """Explicit user action: rebuild only the outline from the canonical transcript."""
    from ..poller.poll import current_daemon_owner
    from ..worker.claims import processing_owner
    from ..worker.pipeline import (
        PipelineAlreadyRunning,
        claim_outline_rebuild,
        processing_claim_active,
        release_processing_claim,
    )

    method = body.method if body is not None else None
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise HTTPException(status_code=404, detail="recording not found")
        if row.local_transcript is None:
            return JSONResponse(
                {"error": "a local transcript is required before an outline"}, status_code=409
            )
        if processing_claim_active(row):
            return JSONResponse({"error": "recording is already processing"}, status_code=409)
    try:
        with processing_owner(current_daemon_owner()):
            claim_token = claim_outline_rebuild(file_id, method=method)
    except PipelineAlreadyRunning:
        return JSONResponse({"error": "recording is already processing"}, status_code=409)
    try:
        _start_worker(_run_rebuild, file_id, claim_token, method)
    except Exception:  # noqa: BLE001
        release_processing_claim(file_id, claim_token)
        return JSONResponse({"error": "could not start the outline rebuild"}, status_code=503)
    return {
        "file_id": file_id,
        "status": "queued",
        "method": method or get_settings().pipeline.outline_method,
    }


class FeedbackRequest(BaseModel):
    value: Literal["up", "down"] | None


@router.post("/api/files/{file_id}/outline/feedback")
def outline_feedback(file_id: str, body: FeedbackRequest) -> dict:
    """Store a local-only thumbs up/down on the live outline (``null`` clears it)."""
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise HTTPException(status_code=404, detail="recording not found")
        live = live_outline(row)
        if live is None:
            raise HTTPException(status_code=404, detail="no outline yet")
        live.feedback = body.value
        live.feedback_at = datetime.now(UTC) if body.value else None
        session.flush()
        return {"file_id": file_id, "revision": live.revision, "feedback": live.feedback}
