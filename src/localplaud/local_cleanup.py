"""Explicit local-only storage cleanup; never mutates Plaud cloud state."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, select, update

from .db.models import (
    Chunk,
    FileStatus,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexAttempt,
    PlaudFile,
    ProviderCostReservation,
    StageAttempt,
    StageRun,
    Summary,
    SummaryRevision,
    Transcript,
    TranscriptRevision,
    UserNote,
)
from .db.session import session_scope
from .providers.usage import lock_cost_budget


def _quarantine(paths: list[str | None]) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []
    seen: set[Path] = set()
    try:
        for value in paths:
            if not value:
                continue
            path = Path(value)
            if path in seen:
                continue
            seen.add(path)
            if not path.exists():
                continue
            quarantined = path.with_name(f".{path.name}.localplaud-delete-{uuid4().hex}")
            path.replace(quarantined)
            moves.append((path, quarantined))
    except Exception:
        _restore_quarantine(moves)
        raise
    return moves


def _restore_quarantine(moves: list[tuple[Path, Path]]) -> None:
    for original, quarantined in reversed(moves):
        if quarantined.exists() and not original.exists():
            quarantined.replace(original)


def _delete_quarantine(moves: list[tuple[Path, Path]]) -> int:
    removed = 0
    for _original, quarantined in moves:
        if quarantined.exists():
            quarantined.unlink()
            removed += 1
    return removed


def _lease_active(value, now: datetime) -> bool:
    if value is None:
        return False
    lease = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return lease > now


def _retain_stage_attempt_spend(session, file_ids: list[str]) -> None:
    """Keep cumulative provider spend after deletable processing history is reset."""
    rows = session.execute(
        select(
            StageAttempt.file_id,
            func.count(StageAttempt.id),
            func.coalesce(func.sum(StageAttempt.estimated_cost_usd), 0),
        )
        .where(StageAttempt.file_id.in_(file_ids))
        .group_by(StageAttempt.file_id)
    ).all()
    now = datetime.now(UTC)
    for file_id, count, estimated_cost in rows:
        cost = float(estimated_cost or 0)
        if cost <= 0:
            continue
        session.add(
            ProviderCostReservation(
                id=f"cleanup:{file_id}:{uuid4().hex}",
                scope_key=f"file:{file_id}",
                file_id=file_id,
                operation="retained-stage-cost",
                status="completed",
                usage={"retained_stage_attempts": int(count)},
                estimated_cost_usd=cost,
                completed_at=now,
            )
        )


def remove_local_audio(file_id: str) -> dict:
    from .worker.pipeline import processing_claim_active

    quarantined: list[tuple[Path, Path]] = []
    try:
        with session_scope() as session:
            lock_cost_budget(session, file_id)
            row = session.get(PlaudFile, file_id)
            if row is None:
                raise LookupError("recording not found")
            if row.origin != "plaud":
                raise ValueError("local uploads cannot be restored after removing their only audio")
            now = datetime.now(UTC)
            download_lease = row.download_lease_until
            if download_lease is not None and download_lease.tzinfo is None:
                download_lease = download_lease.replace(tzinfo=UTC)
            download_active = bool(
                row.download_token
                and download_lease is not None
                and download_lease > now
            )
            if row.status == FileStatus.downloading or download_active:
                raise ValueError("recording audio is currently downloading")
            if processing_claim_active(row) or row.status == FileStatus.processing:
                raise ValueError("recording is currently processing")
            paths = [row.audio_path, row.wav_path]
            parent = Path(row.audio_path).parent if row.audio_path else None
            if parent and parent.exists():
                paths.extend(str(cache) for cache in parent.glob("waveform-*.json"))
            quarantined = _quarantine(paths)
            row.audio_path = None
            row.wav_path = None
            row.downloaded_at = None
            row.download_token = None
            row.download_lease_until = None
            row.status = FileStatus.metadata_only
            row.error = None
    except Exception:
        _restore_quarantine(quarantined)
        raise
    removed = _delete_quarantine(quarantined)
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
    from .worker.knowledge_index import (
        KnowledgeIndexBusyError,
        reject_active_ask_evidence_mutation,
    )
    from .worker.pipeline import processing_claim_active

    unique_ids = list(dict.fromkeys(file_ids))
    if not unique_ids:
        raise ValueError("at least one recording is required")
    quarantined: list[tuple[Path, Path]] = []
    try:
        with session_scope() as session:
            # Cost guards and cleanup use one lock order: recording budget, index
            # document, then attempt. Holding it through validation and quarantine
            # prevents a worker from starting in the former two-transaction gap.
            for file_id in sorted(unique_ids):
                lock_cost_budget(session, file_id)
            rows = list(session.scalars(select(PlaudFile).where(PlaudFile.id.in_(unique_ids))))
            if {row.id for row in rows} != set(unique_ids):
                raise LookupError("recording not found")
            try:
                for file_id in sorted(unique_ids):
                    reject_active_ask_evidence_mutation(session, file_id)
            except KnowledgeIndexBusyError as exc:
                raise ValueError(str(exc)) from exc
            if any(
                processing_claim_active(row) or row.status == FileStatus.processing for row in rows
            ):
                raise ValueError("a selected recording is currently processing")
            wav_paths = [row.wav_path for row in rows]
            local_summaries = list(
                session.scalars(
                    select(Summary)
                    .where(Summary.file_id.in_(unique_ids), Summary.source == "local")
                    .with_for_update()
                )
            )
            local_summary_ids = [summary.id for summary in local_summaries]
            if local_summary_ids:
                session.execute(
                    update(UserNote)
                    .where(UserNote.source_summary_id.in_(local_summary_ids))
                    .values(source_summary_id=None)
                )
                indexed_documents = list(
                    session.scalars(
                        select(KnowledgeDocument)
                        .where(KnowledgeDocument.summary_id.in_(local_summary_ids))
                        .with_for_update()
                    )
                )
                now = datetime.now(UTC)
                if any(
                    document.status == "running" and _lease_active(document.lease_until, now)
                    for document in indexed_documents
                ):
                    raise ValueError("a selected recording is currently indexing notes")
                indexed_document_ids = [document.id for document in indexed_documents]
                # Bulk deletes bypass ORM cascades, and deployed SQLite libraries
                # may have foreign-key enforcement disabled. Remove embedded note
                # text explicitly before deleting its document and Summary source.
                session.execute(
                    delete(KnowledgeChunk).where(
                        KnowledgeChunk.document_id.in_(indexed_document_ids)
                    )
                )
                session.execute(
                    update(KnowledgeIndexAttempt)
                    .where(KnowledgeIndexAttempt.document_id.in_(indexed_document_ids))
                    .values(document_id=None)
                )
                session.execute(
                    delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(indexed_document_ids))
                )
            _retain_stage_attempt_spend(session, unique_ids)
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
                "note_versions": session.execute(
                    delete(SummaryRevision).where(
                        SummaryRevision.file_id.in_(unique_ids),
                        SummaryRevision.source == "local",
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
            quarantined = _quarantine(wav_paths)
    except Exception:
        _restore_quarantine(quarantined)
        raise
    removed_files = _delete_quarantine(quarantined)
    return {
        "file_ids": unique_ids,
        "removed_files": removed_files,
        "removed": counts,
        "statuses": statuses,
    }
