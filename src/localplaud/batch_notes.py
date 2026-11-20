"""Library batch "Generate notes": durably queue note-only regeneration.

A batch request never runs ASR, never calls Plaud, and never starts work in the
request thread. Each eligible recording gets the same durable derived-only queue
marker used by ``reprocess-all --mode derived_only``; the worker loop drains it
under the shared ``pipeline.concurrency`` admission and the automatic
template/AI-profile resolution (including its privacy/fallback policy).

Eligibility mirrors the single-recording Generate notes entry point: the
recording must be outside Trash, have a canonical *local* transcript (Plaud
transcripts never qualify), and not be processing. Recordings whose regeneration
is already queued are reported rather than invalidated again.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from .config import Settings, get_settings
from .db.models import FileStatus, PlaudFile, StageName, StageStatus
from .db.session import session_scope
from .preferences import get_workspace_preferences
from .providers.usage import lock_cost_budget

MAX_BATCH_NOTE_FILES = 50

_MESSAGES = {
    "trash": "In Trash. Restore it before generating notes.",
    "no_local_transcript": "No local transcript yet. Process the recording first.",
    "no_speech": "The transcript has no recognizable speech.",
    "processing": "Already processing. Try again when it finishes.",
    "evidence_in_use": "Recording evidence is in use. Try again when it finishes.",
    "notes_disabled": "Note generation is disabled in the automatic processing settings.",
    "automatic_paused": (
        "Automatic processing is off. Open the recording to generate notes."
    ),
    "automatic_restricted": (
        "Automatic processing is limited to untranscribed recordings. "
        "Open the recording to generate notes."
    ),
}


class UnknownRecordings(LookupError):
    """At least one requested recording is not visible in this workspace."""


def _already_queued(row: PlaudFile) -> bool:
    if row.status != FileStatus.partial or (row.pipeline_retry_count or 0) != 0:
        return False
    return any(
        run.stage == StageName.summarize
        and run.status == StageStatus.pending
        and bool((run.detail or {}).get("derived_only"))
        for run in row.stage_runs
    )


def _ineligible_reason(
    row: PlaudFile, settings: Settings, now: datetime, *, automatic_enabled: bool
) -> str | None:
    from .worker.pipeline import _recently_transcribed, processing_claim_active

    if row.is_trash:
        return "trash"
    local = row.local_transcript
    if local is None:
        return "no_local_transcript"
    canonical = row.corrected_transcript_for_source("local") or local
    if not (canonical.text or "").strip():
        return "no_speech"
    if processing_claim_active(row, now=now) or row.status == FileStatus.processing:
        return "processing"
    if not automatic_enabled:
        return "automatic_paused"
    if not settings.pipeline.summarize:
        return "notes_disabled"
    # In this mode the daemon would never admit an older transcript, so queueing
    # it would strand the request silently.
    if settings.pipeline.auto_process_untranscribed_only and not _recently_transcribed(
        row, settings, now
    ):
        return "automatic_restricted"
    return None


def queue_batch_note_generation(
    file_ids: list[str],
    *,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> dict:
    """Classify, and unless ``dry_run`` queue, note-only regeneration.

    Raises :class:`UnknownRecordings` (nothing changes) when any id is not
    visible in the active workspace, and ``ValueError`` for an empty or
    oversized selection.
    """
    from .worker.knowledge_index import KnowledgeIndexBusyError, reject_active_ask_evidence_mutation
    from .worker.pipeline import queue_derived_regeneration, reset_pipeline_retry

    settings = settings or get_settings()
    ids = list(dict.fromkeys(file_ids))
    if not ids:
        raise ValueError("select at least one recording")
    if len(ids) > MAX_BATCH_NOTE_FILES:
        raise ValueError(f"select no more than {MAX_BATCH_NOTE_FILES} recordings")
    eligible: list[str] = []
    already: list[str] = []
    skipped: list[dict] = []
    with session_scope() as session:
        if not dry_run:
            lock_cost_budget(session, None)
        now = datetime.now(UTC)
        rows = {
            row.id: row for row in session.scalars(select(PlaudFile).where(PlaudFile.id.in_(ids)))
        }
        if set(rows) != set(ids):
            raise UnknownRecordings("recording not found")
        automatic_enabled = get_workspace_preferences(session)["auto_process_new_recordings"]
        for file_id in ids:
            row = rows[file_id]
            reason = _ineligible_reason(row, settings, now, automatic_enabled=automatic_enabled)
            already_queued = reason is None and _already_queued(row)
            if reason is None and not already_queued:
                try:
                    reject_active_ask_evidence_mutation(session, file_id)
                except KnowledgeIndexBusyError:
                    reason = "evidence_in_use"
            if reason is not None:
                skipped.append(
                    {
                        "file_id": row.id,
                        "title": row.display_title,
                        "reason": reason,
                        "message": _MESSAGES[reason],
                    }
                )
            elif already_queued:
                already.append(row.id)
            else:
                eligible.append(row.id)
        if not dry_run:
            for file_id in eligible:
                row = rows[file_id]
                reset_pipeline_retry(row)
                queue_derived_regeneration(
                    session, row, reason="library batch generate notes", preserve_transcript=True
                )
                # Queue state is not an error; make it due immediately.
                row.error = None
                row.pipeline_last_failure_at = now
                row.pipeline_next_retry_at = now
    return {
        "dry_run": dry_run,
        "requested": len(ids),
        ("eligible" if dry_run else "queued"): eligible,
        "already_queued": already,
        "skipped": skipped,
        "max_files": MAX_BATCH_NOTE_FILES,
    }
