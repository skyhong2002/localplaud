"""Explicit local-only storage cleanup; never mutates Plaud cloud state."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, select, update

from .db.models import (
    Chunk,
    FileStatus,
    PlaudFile,
    StageAttempt,
    StageRun,
    Summary,
    Transcript,
    TranscriptRevision,
    UserNote,
)
from .db.session import session_scope


def _unlink(paths: list[str | None]) -> int:
    removed = 0
    seen: set[Path] = set()
    for value in paths:
        if not value:
            continue
        path = Path(value)
        if path in seen:
            continue
        seen.add(path)
        if path.exists():
            path.unlink()
            removed += 1
    return removed


def remove_local_audio(file_id: str) -> dict:
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise LookupError("recording not found")
        if row.origin != "plaud":
            raise ValueError("local uploads cannot be restored after removing their only audio")
        paths = [row.audio_path, row.wav_path]
        parent = Path(row.audio_path).parent if row.audio_path else None
    removed = _unlink(paths)
    if parent and parent.exists():
        for cache in parent.glob("waveform-*.json"):
            cache.unlink(missing_ok=True)
            removed += 1
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        row.audio_path = None
        row.wav_path = None
        row.downloaded_at = None
        row.status = FileStatus.metadata_only
        row.error = None
    return {"file_id": file_id, "removed_files": removed, "status": "metadata_only"}


def delete_local_processing(file_id: str) -> dict:
    result = delete_local_processing_many([file_id])
    return {
        "file_id": file_id,
        "removed_files": result["removed_files"],
        "removed": result["removed"],
        "status": result["statuses"][file_id],
    }


def delete_local_processing_many(file_ids: list[str]) -> dict:
    """Atomically remove local derivatives for validated, idle recordings."""
    from .worker.pipeline import processing_claim_active

    unique_ids = list(dict.fromkeys(file_ids))
    if not unique_ids:
        raise ValueError("at least one recording is required")
    with session_scope() as session:
        rows = list(
            session.scalars(select(PlaudFile).where(PlaudFile.id.in_(unique_ids)))
        )
        if {row.id for row in rows} != set(unique_ids):
            raise LookupError("recording not found")
        if any(processing_claim_active(row) or row.status == FileStatus.processing for row in rows):
            raise ValueError("a selected recording is currently processing")
        wav_paths = [row.wav_path for row in rows]
        local_summary_ids = list(
            session.scalars(
                select(Summary.id).where(
                    Summary.file_id.in_(unique_ids), Summary.source == "local"
                )
            )
        )
    removed_files = _unlink(wav_paths)
    with session_scope() as session:
        if local_summary_ids:
            session.execute(
                update(UserNote)
                .where(UserNote.source_summary_id.in_(local_summary_ids))
                .values(source_summary_id=None)
            )
        counts = {
            "revisions": session.execute(
                delete(TranscriptRevision).where(
                    TranscriptRevision.file_id.in_(unique_ids),
                    TranscriptRevision.source == "local",
                )
            ).rowcount,
            "transcripts": session.execute(
                delete(Transcript).where(
                    Transcript.file_id.in_(unique_ids), Transcript.source == "local"
                )
            ).rowcount,
            "notes": session.execute(
                delete(Summary).where(
                    Summary.file_id.in_(unique_ids), Summary.source == "local"
                )
            ).rowcount,
            "chunks": session.execute(
                delete(Chunk).where(Chunk.file_id.in_(unique_ids))
            ).rowcount,
            "stages": session.execute(
                delete(StageRun).where(StageRun.file_id.in_(unique_ids))
            ).rowcount,
            "attempts": session.execute(
                delete(StageAttempt).where(StageAttempt.file_id.in_(unique_ids))
            ).rowcount,
        }
        rows = list(
            session.scalars(select(PlaudFile).where(PlaudFile.id.in_(unique_ids)))
        )
        statuses: dict[str, str] = {}
        for row in rows:
            row.wav_path = None
            row.error = None
            row.processing_token = None
            row.processing_lease_until = None
            row.pipeline_retry_count = 0
            row.pipeline_next_retry_at = None
            row.pipeline_last_failure_at = None
            has_audio = bool(row.audio_path and Path(row.audio_path).exists())
            row.status = FileStatus.downloaded if has_audio else FileStatus.metadata_only
            statuses[row.id] = row.status.value
    return {
        "file_ids": unique_ids,
        "removed_files": removed_files,
        "removed": counts,
        "statuses": statuses,
    }
