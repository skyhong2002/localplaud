"""Explicit local-only storage cleanup; never mutates Plaud cloud state."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete

from .db.models import (
    Chunk,
    FileStatus,
    PlaudFile,
    StageRun,
    Summary,
    Transcript,
    TranscriptRevision,
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
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise LookupError("recording not found")
        wav_path = row.wav_path
    removed_files = _unlink([wav_path])
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        counts = {
            "revisions": session.execute(
                delete(TranscriptRevision).where(
                    TranscriptRevision.file_id == file_id,
                    TranscriptRevision.source == "local",
                )
            ).rowcount,
            "transcripts": session.execute(
                delete(Transcript).where(
                    Transcript.file_id == file_id, Transcript.source == "local"
                )
            ).rowcount,
            "notes": session.execute(
                delete(Summary).where(
                    Summary.file_id == file_id, Summary.source == "local"
                )
            ).rowcount,
            "chunks": session.execute(delete(Chunk).where(Chunk.file_id == file_id)).rowcount,
            "stages": session.execute(
                delete(StageRun).where(StageRun.file_id == file_id)
            ).rowcount,
        }
        row.wav_path = None
        row.error = None
        has_audio = bool(row.audio_path and Path(row.audio_path).exists())
        row.status = FileStatus.downloaded if has_audio else FileStatus.metadata_only
    return {
        "file_id": file_id,
        "removed_files": removed_files,
        "removed": counts,
        "status": row.status.value,
    }
