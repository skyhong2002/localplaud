"""Durable semantic index for generated and user-authored notes.

Transcript chunks have timestamp and speaker semantics that note evidence does
not.  Notes therefore use their own document/chunk tables and are projected into
the shared Ask ranking only at retrieval time.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import delete, exists, or_, select, update

from ..config import Settings, get_settings
from ..db.models import (
    AskThread,
    Chunk,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexAttempt,
    PlaudFile,
    ProviderConnection,
    ProviderCostReservation,
    StageAttempt,
    StageName,
    StageRun,
    StageStatus,
    Summary,
    UserNote,
)
from ..db.session import session_scope
from ..embeddings.base import build_embedder
from ..error_redaction import sanitize_error
from ..note_history import fingerprint_digest
from ..providers.fallback import candidate_snapshots, is_retryable_fallback_error
from ..providers.service import preview_resolution, resolve_recording_profile
from ..providers.usage import (
    CostPolicyError,
    finalize_provider_cost_reservations,
    lock_cost_budget,
    provider_cost_reservation_total,
    provider_dispatch_fingerprint,
    reserve_provider_cost,
)

log = logging.getLogger(__name__)

_locks_guard = threading.Lock()
_document_locks: dict[int, threading.Lock] = {}
_LOCAL_INDEX_LEASE = timedelta(minutes=30)
_REMOTE_INDEX_LEASE_BUFFER = timedelta(minutes=5)
_DEFAULT_REMOTE_JOB_TIMEOUT_SECONDS = 3600.0


class KnowledgeIndexBusyError(RuntimeError):
    pass


def _document_claim_active(
    document: KnowledgeDocument, *, now: datetime | None = None
) -> bool:
    return bool(
        document.status == "running"
        and document.lease_token
        and document.lease_until is not None
        and _as_utc(document.lease_until) > (now or datetime.now(UTC))
    )


def _document_lock(document_id: int) -> threading.Lock:
    with _locks_guard:
        return _document_locks.setdefault(document_id, threading.Lock())


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def embedding_identity(snapshot: dict | None) -> dict:
    selection = ((snapshot or {}).get("stages") or {}).get("embed") or {}
    return {
        key: selection.get(key)
        for key in (
            "connection",
            "model",
            "provider_type",
            "execution_target",
            "configuration",
            "options",
        )
    }


def _resolved_embedding_snapshot(session, file_id: str | None) -> dict:
    return (
        resolve_recording_profile(session, file_id).to_dict()
        if file_id
        else preview_resolution(session).to_dict()
    )


def _profile_changed(session, document: KnowledgeDocument) -> bool:
    if not document.profile_snapshot:
        # New documents intentionally have no provenance until their first claim.
        # A completed legacy document without it cannot prove its embedding space.
        return document.status == "completed"
    current = _resolved_embedding_snapshot(session, document.file_id)
    current_identities = [
        embedding_identity(candidate) for candidate in candidate_snapshots(current, "embed")
    ]
    return embedding_identity(document.profile_snapshot) not in current_identities


def _claim_lease_duration(session, snapshot: dict) -> timedelta:
    """Keep a claim through every sequential embedding candidate."""
    lease = timedelta()
    for candidate in candidate_snapshots(snapshot, "embed"):
        selection = (candidate.get("stages") or {}).get("embed") or {}
        if selection.get("execution_target") != "remote_worker":
            lease += _LOCAL_INDEX_LEASE
            continue
        connection = session.scalar(
            select(ProviderConnection).where(ProviderConnection.key == selection.get("connection"))
        )
        config = dict(connection.config or {}) if connection is not None else {}
        try:
            timeout_seconds = float(config.get("job_timeout", _DEFAULT_REMOTE_JOB_TIMEOUT_SECONDS))
        except (TypeError, ValueError):
            timeout_seconds = _DEFAULT_REMOTE_JOB_TIMEOUT_SECONDS
        if timeout_seconds <= 0:
            timeout_seconds = _DEFAULT_REMOTE_JOB_TIMEOUT_SECONDS
        lease += timedelta(seconds=timeout_seconds) + _REMOTE_INDEX_LEASE_BUFFER
    return max(_LOCAL_INDEX_LEASE, lease)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _lock_document(session, document_id: int) -> KnowledgeDocument | None:
    if session.get_bind().dialect.name == "sqlite":
        session.execute(
            update(KnowledgeDocument)
            .where(KnowledgeDocument.id == document_id)
            .values(id=KnowledgeDocument.id)
        )
        return session.get(KnowledgeDocument, document_id)
    return session.scalar(
        select(KnowledgeDocument).where(KnowledgeDocument.id == document_id).with_for_update()
    )


def _delete_document(session, document: KnowledgeDocument) -> None:
    """Delete note index data explicitly even when SQLite FK checks are off."""
    session.execute(
        update(KnowledgeIndexAttempt)
        .where(KnowledgeIndexAttempt.document_id == document.id)
        .values(document_id=None)
    )
    session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
    session.delete(document)


def _lock_artifact(session, artifact):
    if session.get_bind().dialect.name == "sqlite":
        session.execute(
            update(type(artifact)).where(type(artifact).id == artifact.id).values(id=artifact.id)
        )
        session.refresh(artifact)
        return artifact
    return session.scalar(
        select(type(artifact))
        .where(type(artifact).id == artifact.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _lock_user_note_mutation_boundary(
    session, note_id: int, file_id: str | None
) -> tuple[UserNote | None, KnowledgeDocument | None]:
    # Every note mutation and provider boundary uses this order to prevent a
    # committed edit/delete from being followed by stale provider egress.
    with session.no_autoflush:
        lock_cost_budget(
            session,
            None if session.get_bind().dialect.name == "sqlite" else file_id,
        )
    note = session.get(UserNote, note_id)
    if note is None:
        return None, None
    note = _lock_artifact(session, note)
    if note is None:
        return None, None
    document_id = session.scalar(
        select(KnowledgeDocument.id).where(KnowledgeDocument.user_note_id == note.id)
    )
    document = _lock_document(session, document_id) if document_id is not None else None
    return note, document


def _lock_summary_mutation_boundary(
    session, summary_id: int, file_id: str
) -> tuple[Summary | None, KnowledgeDocument | None]:
    with session.no_autoflush:
        lock_cost_budget(session, file_id)
    summary = session.get(Summary, summary_id)
    if summary is None:
        return None, None
    summary = _lock_artifact(session, summary)
    if summary is None:
        return None, None
    document_id = session.scalar(
        select(KnowledgeDocument.id).where(KnowledgeDocument.summary_id == summary.id)
    )
    document = _lock_document(session, document_id) if document_id is not None else None
    return summary, document


def reject_active_ask_evidence_mutation(session, file_id: str | None) -> None:
    """Reject evidence/scope mutation while a relevant durable Ask lease is active.

    Callers must first acquire the matching cost-budget lock. Ask request claims
    use the same lock order, so the committed lease and this check cannot race.
    ``None`` covers library-wide metadata mutations; a recording id covers both
    that recording's Ask surface and every active library Ask.
    """
    now = datetime.now(UTC)
    scope = AskThread.file_id.is_(None)
    if file_id is not None:
        scope = or_(scope, AskThread.file_id == file_id)
    active = session.scalar(
        select(AskThread.id)
        .where(
            scope,
            AskThread.request_token.is_not(None),
            AskThread.request_lease_until.is_not(None),
            AskThread.request_lease_until > now,
        )
        .limit(1)
        .with_for_update()
    )
    if active is not None:
        raise KnowledgeIndexBusyError(
            "recording evidence is currently being used by Ask; try again when it finishes"
        )


def lock_summary_for_mutation(session, summary_id: int, file_id: str) -> Summary | None:
    summary, document = _lock_summary_mutation_boundary(session, summary_id, file_id)
    if summary is not None:
        reject_active_ask_evidence_mutation(session, file_id)
    if document is not None and _document_claim_active(document):
        raise KnowledgeIndexBusyError("note is currently indexing; try again when it finishes")
    return summary


def lock_user_note_for_mutation(session, note_id: int) -> UserNote | None:
    if session.get_bind().dialect.name == "sqlite":
        locked, document = _lock_user_note_mutation_boundary(session, note_id, None)
        if locked is not None:
            reject_active_ask_evidence_mutation(session, locked.file_id)
        if document is not None and _document_claim_active(document):
            raise KnowledgeIndexBusyError("note is currently indexing; try again when it finishes")
        return locked
    note = session.get(UserNote, note_id)
    if note is None:
        return None
    locked, document = _lock_user_note_mutation_boundary(session, note_id, note.file_id)
    if locked is not None:
        reject_active_ask_evidence_mutation(session, locked.file_id)
    if document is not None and _document_claim_active(document):
        raise KnowledgeIndexBusyError("note is currently indexing; try again when it finishes")
    return locked


def delete_user_note_document(session, note: UserNote) -> None:
    locked, document = _lock_user_note_mutation_boundary(session, note.id, note.file_id)
    if locked is not None and document is not None:
        _delete_document(session, document)


def _lock_document_source_then_document(session, document_id: int) -> KnowledgeDocument | None:
    document = session.get(KnowledgeDocument, document_id)
    if document is None:
        return None
    artifact = (
        session.get(Summary, document.summary_id)
        if document.kind == "generated_summary"
        else session.get(UserNote, document.user_note_id)
    )
    if artifact is not None:
        _lock_artifact(session, artifact)
    return _lock_document(session, document_id)


def _generated_note_payload(summary: Summary) -> dict:
    return {
        "title": summary.title,
        "content_md": summary.content_md,
        "fingerprint": fingerprint_digest(summary),
        "restored_from_revision": summary.restored_from_revision,
    }


def _user_note_payload(note: UserNote) -> dict:
    return {
        "title": note.title,
        "content_md": note.content_md,
        "version": note.version,
        "source_type": note.source_type,
        "source_summary_snapshot": note.source_summary_snapshot or {},
    }


def _summary_is_current(
    session,
    summary: Summary,
    settings: Settings,
    *,
    allow_running_stage: bool = False,
) -> bool:
    """Fail closed unless a generated note is proven local and current."""
    if summary.source != "local" or summary.template == "mind_map":
        return False
    row = session.get(PlaudFile, summary.file_id)
    if row is None:
        return False
    summarize_run = session.scalar(
        select(StageRun).where(
            StageRun.file_id == summary.file_id,
            StageRun.stage == StageName.summarize,
        )
    )
    if (
        summarize_run is not None
        and bool((summarize_run.detail or {}).get("stale"))
        and not (allow_running_stage and str(summarize_run.status.value) == "running")
    ):
        return False
    from .pipeline import _select_raw_transcript

    raw = _select_raw_transcript(row, settings)
    if raw is None or raw.source != "local":
        return False
    revision = row.corrected_transcript_for_source(raw.source)
    return bool(
        summary.input_transcript_source == "local"
        and summary.input_transcript_id == raw.id
        and summary.input_transcript_revision == (revision.revision if revision else 0)
    )


def _user_note_is_allowed(session, note: UserNote, settings: Settings) -> bool:
    if note.source_type != "generated_summary":
        return True
    snapshot = note.source_summary_snapshot or {}
    if (
        snapshot.get("source") == "local"
        and snapshot.get("input_transcript_source") == "local"
        and isinstance(snapshot.get("input_transcript_id"), int)
        and isinstance(snapshot.get("input_transcript_revision"), int)
        and isinstance(snapshot.get("content_fingerprint"), str)
        and len(snapshot["content_fingerprint"]) == 64
    ):
        return True
    if note.source_summary_id is None:
        return False
    summary = session.get(Summary, note.source_summary_id)
    return bool(
        summary is not None
        and _summary_is_current(session, summary, settings)
        and snapshot.get("content_fingerprint") == fingerprint_digest(summary)
    )


def knowledge_document_is_current(
    session,
    document: KnowledgeDocument,
    settings: Settings | None = None,
    *,
    allow_running_stage: bool = False,
) -> bool:
    settings = settings or get_settings()
    if document.kind == "generated_summary":
        summary = session.get(Summary, document.summary_id)
        return bool(
            summary is not None
            and _summary_is_current(
                session,
                summary,
                settings,
                allow_running_stage=allow_running_stage,
            )
            and document.content_sha256 == _digest(_generated_note_payload(summary))
        )
    if document.kind == "user_note":
        note = session.get(UserNote, document.user_note_id)
        return bool(
            note is not None
            and _user_note_is_allowed(session, note, settings)
            and document.artifact_version == note.version
            and document.content_sha256 == _digest(_user_note_payload(note))
        )
    return False


def _reset_document(document: KnowledgeDocument, content_sha256: str, version: int | None) -> None:
    document.content_sha256 = content_sha256
    document.artifact_version = version
    document.generation = secrets.token_hex(16)
    document.status = "pending"
    document.attempts = 0
    document.lease_token = None
    document.lease_until = None
    document.next_retry_at = None
    document.error = None
    document.provider = None
    document.model = None
    document.dim = None
    document.profile_snapshot = None
    document.indexed_at = None


def sync_summary_document(
    session,
    summary: Summary,
    settings: Settings | None = None,
    *,
    allow_running_stage: bool = False,
) -> KnowledgeDocument | None:
    settings = settings or get_settings()
    summary, document = _lock_summary_mutation_boundary(session, summary.id, summary.file_id)
    if summary is None:
        return None
    if not _summary_is_current(session, summary, settings, allow_running_stage=allow_running_stage):
        if document is not None:
            _delete_document(session, document)
        return None
    digest = _digest(_generated_note_payload(summary))
    if document is None:
        document = KnowledgeDocument(
            kind="generated_summary",
            file_id=summary.file_id,
            summary_id=summary.id,
            user_note_id=None,
            artifact_version=summary.restored_from_revision or 0,
            content_sha256=digest,
            generation=secrets.token_hex(16),
            status="pending",
        )
        session.add(document)
        session.flush()
    elif document.content_sha256 != digest:
        session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        _reset_document(document, digest, summary.restored_from_revision or 0)
    elif _profile_changed(session, document):
        session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        _reset_document(document, digest, summary.restored_from_revision or 0)
    return document


def sync_user_note_document(
    session, note: UserNote, settings: Settings | None = None
) -> KnowledgeDocument | None:
    settings = settings or get_settings()
    note, document = _lock_user_note_mutation_boundary(session, note.id, note.file_id)
    if note is None:
        return None
    if not _user_note_is_allowed(session, note, settings):
        if document is not None:
            _delete_document(session, document)
        return None
    digest = _digest(_user_note_payload(note))
    if document is None:
        document = KnowledgeDocument(
            kind="user_note",
            file_id=note.file_id,
            summary_id=None,
            user_note_id=note.id,
            artifact_version=note.version,
            content_sha256=digest,
            generation=secrets.token_hex(16),
            status="pending",
        )
        session.add(document)
        session.flush()
    elif document.content_sha256 != digest or document.artifact_version != note.version:
        session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        document.file_id = note.file_id
        _reset_document(document, digest, note.version)
    elif _profile_changed(session, document):
        session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        _reset_document(document, digest, note.version)
    return document


def sync_knowledge_documents(session, settings: Settings | None = None) -> list[int]:
    """Create pending documents for current artifacts without embedding them."""
    settings = settings or get_settings()
    # Global synchronization can touch library notes plus any recording. Acquire
    # the same deterministic budget order used by bulk cleanup before artifact
    # locks, so PostgreSQL callers cannot deadlock on opposite scan orders.
    lock_cost_budget(session, None)
    for file_id in session.scalars(select(PlaudFile.id).order_by(PlaudFile.id)):
        lock_cost_budget(session, file_id)
    # Some deployed SQLite libraries historically ran with foreign-key
    # enforcement disabled. Clean up bounded index metadata even when an
    # external/bulk artifact delete could not cascade at the database layer.
    orphan_ids = select(KnowledgeDocument.id).where(
        or_(
            (
                (KnowledgeDocument.kind == "generated_summary")
                & ~exists(select(Summary.id).where(Summary.id == KnowledgeDocument.summary_id))
            ),
            (
                (KnowledgeDocument.kind == "user_note")
                & ~exists(select(UserNote.id).where(UserNote.id == KnowledgeDocument.user_note_id))
            ),
        )
    )
    session.execute(
        update(KnowledgeIndexAttempt)
        .where(KnowledgeIndexAttempt.document_id.in_(orphan_ids))
        .values(document_id=None)
    )
    session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id.in_(orphan_ids)))
    session.execute(delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(orphan_ids)))
    ids: list[int] = []
    for summary in session.scalars(select(Summary).order_by(Summary.file_id, Summary.id)):
        document = sync_summary_document(session, summary, settings, allow_running_stage=True)
        if document is not None:
            ids.append(document.id)
    for note in session.scalars(select(UserNote).order_by(UserNote.file_id, UserNote.id)):
        document = sync_user_note_document(session, note, settings)
        if document is not None:
            ids.append(document.id)
    sync_transcript_index_profiles(session, settings=settings)
    return ids


def invalidate_generated_documents(session, file_id: str) -> None:
    """Remove generated-note evidence when its summarization input is stale."""
    document_ids = select(KnowledgeDocument.id).where(
        KnowledgeDocument.file_id == file_id,
        KnowledgeDocument.kind == "generated_summary",
    )
    session.execute(
        update(KnowledgeIndexAttempt)
        .where(KnowledgeIndexAttempt.document_id.in_(document_ids))
        .values(document_id=None)
    )
    session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id.in_(document_ids)))
    session.execute(
        delete(KnowledgeDocument).where(
            KnowledgeDocument.file_id == file_id,
            KnowledgeDocument.kind == "generated_summary",
        )
    )


def sync_file_knowledge_documents(
    session, file_id: str, settings: Settings | None = None
) -> list[int]:
    settings = settings or get_settings()
    if session.get_bind().dialect.name == "sqlite":
        lock_cost_budget(session, file_id)
    row = session.get(PlaudFile, file_id)
    if row is None:
        return []
    documents: list[KnowledgeDocument] = []
    for summary in row.summaries:
        document = sync_summary_document(session, summary, settings, allow_running_stage=True)
        if document is not None:
            documents.append(document)
    for note in row.user_notes:
        document = sync_user_note_document(session, note, settings)
        if document is not None:
            documents.append(document)
    sync_transcript_index_profiles(session, file_ids=[file_id], settings=settings)
    return [document.id for document in documents]


def sync_transcript_index_profiles(
    session,
    *,
    file_ids: list[str] | None = None,
    settings: Settings | None = None,
) -> list[str]:
    """Fail closed and durably requeue transcript vectors in obsolete spaces."""
    settings = settings or get_settings()
    stmt = select(Chunk.file_id).where(Chunk.embedding.is_not(None)).distinct()
    if file_ids is not None:
        stmt = stmt.where(Chunk.file_id.in_(file_ids))
    changed: list[str] = []
    for file_id in list(session.scalars(stmt)):
        resolved = _resolved_embedding_snapshot(session, file_id)
        allowed = [
            embedding_identity(candidate) for candidate in candidate_snapshots(resolved, "embed")
        ]
        snapshots = list(
            session.scalars(select(Chunk.resolved_profile_snapshot).where(Chunk.file_id == file_id))
        )
        if snapshots and all(
            snapshot is not None and embedding_identity(snapshot) in allowed
            for snapshot in snapshots
        ):
            continue
        session.execute(delete(Chunk).where(Chunk.file_id == file_id))
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == file_id,
                StageRun.stage == StageName.index,
            )
        )
        if run is None:
            run = StageRun(
                file_id=file_id,
                stage=StageName.index,
                attempts=0,
                detail={},
            )
            session.add(run)
        elif run.status == StageStatus.running:
            attempt = session.scalar(
                select(StageAttempt).where(
                    StageAttempt.file_id == file_id,
                    StageAttempt.stage == StageName.index,
                    StageAttempt.attempt == run.attempts,
                    StageAttempt.status == StageStatus.running,
                )
            )
            if attempt is not None:
                attempt.status = StageStatus.skipped
                attempt.error = "superseded by an embedding profile change"
                attempt.completed_at = datetime.now(UTC)
        run.status = StageStatus.pending
        run.error = None
        run.completed_at = None
        run.detail = dict(run.detail or {}) | {
            "stale": True,
            "stale_generation": secrets.token_hex(16),
            "reason": "embedding profile changed or index provenance is missing",
            "reindex_only": True,
        }
        changed.append(file_id)
    return changed


def build_note_chunks(title: str, content: str, target_chars: int = 700) -> list[str]:
    """Build bounded note passages while keeping Markdown paragraphs coherent."""
    heading = title.strip()
    paragraphs = [part.strip() for part in content.split("\n\n") if part.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if current:
            body = "\n\n".join(current)
            chunks.append(f"{heading}\n\n{body}" if heading else body)
        current, current_chars = [], 0

    for paragraph in paragraphs:
        pieces = [
            paragraph[offset : offset + target_chars]
            for offset in range(0, len(paragraph), target_chars)
        ] or [paragraph]
        for piece in pieces:
            if current and current_chars + len(piece) > target_chars:
                flush()
            current.append(piece)
            current_chars += len(piece)
    flush()
    if not chunks and heading:
        chunks.append(heading)
    return chunks


def _claim_document(document_id: int, settings: Settings) -> dict | None:
    now = datetime.now(UTC)
    with session_scope() as session:
        file_id = session.scalar(
            select(KnowledgeDocument.file_id).where(KnowledgeDocument.id == document_id)
        )
        lock_cost_budget(session, file_id)
        document = _lock_document(session, document_id)
        if document is None or not knowledge_document_is_current(
            session,
            document,
            settings,
            allow_running_stage=True,
        ):
            if document is not None:
                _delete_document(session, document)
            return None
        if document.status == "completed":
            return None
        if (
            document.status == "running"
            and document.lease_until
            and _as_utc(document.lease_until) > now
        ):
            return None
        if document.status == "running":
            displaced = session.scalar(
                select(KnowledgeIndexAttempt)
                .where(
                    KnowledgeIndexAttempt.document_id == document.id,
                    KnowledgeIndexAttempt.generation == document.generation,
                    KnowledgeIndexAttempt.attempt == document.attempts,
                    KnowledgeIndexAttempt.status == "running",
                )
                .order_by(KnowledgeIndexAttempt.id.desc())
            )
            if displaced is not None:
                reservation_ids = list(
                    (displaced.usage or {}).get("dispatch_reservation_ids") or []
                )
                active_dispatch = session.scalar(
                    select(ProviderCostReservation.id)
                    .where(
                        ProviderCostReservation.id.in_(reservation_ids),
                        ProviderCostReservation.status == "active",
                        ProviderCostReservation.lease_until.is_not(None),
                        ProviderCostReservation.lease_until > now,
                    )
                    .limit(1)
                )
                if active_dispatch is not None:
                    return None
                reserved_cost = _settle_document_dispatch_reservations(session, displaced)
                displaced.status = "skipped"
                displaced.error = "note index lease expired and was reclaimed"
                displaced.estimated_cost_usd = max(
                    float(displaced.estimated_cost_usd or 0), reserved_cost
                )
                displaced.completed_at = now
        if document.next_retry_at and _as_utc(document.next_retry_at) > now:
            return None
        if document.kind == "generated_summary":
            artifact = session.get(Summary, document.summary_id)
            title = artifact.title or artifact.template.replace("-", " ").title()
            content = artifact.content_md
        else:
            artifact = session.get(UserNote, document.user_note_id)
            title, content = artifact.title, artifact.content_md
        snapshot = (
            resolve_recording_profile(session, document.file_id).to_dict()
            if document.file_id
            else preview_resolution(session).to_dict()
        )
        token = secrets.token_hex(16)
        document.status = "running"
        document.attempts += 1
        document.lease_token = token
        document.lease_until = now + _claim_lease_duration(session, snapshot)
        document.error = None
        document.profile_snapshot = snapshot
        attempt = KnowledgeIndexAttempt(
            document_id=document.id,
            file_id=document.file_id,
            generation=document.generation,
            attempt=document.attempts,
            status="running",
            usage={},
        )
        session.add(attempt)
        session.flush()
        return {
            "id": document.id,
            "file_id": document.file_id,
            "attempt_id": attempt.id,
            "generation": document.generation,
            "content_sha256": document.content_sha256,
            "lease_token": token,
            "title": title,
            "content": content,
            "snapshot": snapshot,
        }


def _revalidate_claim_for_dispatch(session, claim: dict, settings: Settings) -> bool:
    """Fence provider dispatch after a claim has been superseded or removed."""
    document = _lock_document_source_then_document(session, claim["id"])
    now = datetime.now(UTC)
    valid = bool(
        document is not None
        and document.status == "running"
        and document.generation == claim["generation"]
        and document.content_sha256 == claim["content_sha256"]
        and document.lease_token == claim["lease_token"]
        and document.lease_until is not None
        and _as_utc(document.lease_until) > now
        and knowledge_document_is_current(
            session,
            document,
            settings,
            allow_running_stage=True,
        )
    )
    attempt = session.get(KnowledgeIndexAttempt, claim["attempt_id"])
    valid = valid and bool(attempt is not None and attempt.status == "running")
    if valid:
        return True
    if attempt is not None and attempt.status == "running":
        reserved_cost = _settle_document_dispatch_reservations(session, attempt)
        attempt.status = "skipped"
        attempt.error = "superseded before note index provider dispatch"
        attempt.estimated_cost_usd = max(
            float(attempt.estimated_cost_usd or 0), reserved_cost
        )
        attempt.completed_at = now
    if document is not None and document.lease_token == claim["lease_token"]:
        document.status = "pending"
        document.lease_token = None
        document.lease_until = None
    return False


def _embedding_cost_guard(session, snapshot: dict, input_chars: int, claim: dict) -> dict:
    lock_cost_budget(session, claim.get("file_id"))
    current = (
        resolve_recording_profile(session, claim["file_id"]).to_dict()
        if claim.get("file_id") is not None
        else preview_resolution(session).to_dict()
    )
    authorized = {
        provider_dispatch_fingerprint(candidate, "embed")
        for candidate in candidate_snapshots(current, "embed")
    }
    if provider_dispatch_fingerprint(snapshot, "embed") not in authorized:
        raise RuntimeError(
            "note embedding provider profile changed before dispatch; retry indexing"
        )
    attempt = session.get(KnowledgeIndexAttempt, claim["attempt_id"])
    if attempt is None:
        raise RuntimeError("note index attempt disappeared before cost reservation")
    usage = {"input_chars": input_chars, "requests": 1}
    fingerprint = provider_dispatch_fingerprint(snapshot, "embed")
    reservation_id = f"note:{claim['attempt_id']}:{fingerprint[:24]}"
    try:
        projected, pricing = reserve_provider_cost(
            session,
            reservation_id=reservation_id,
            file_id=claim.get("file_id"),
            operation="embed",
            snapshot=snapshot,
            usage=usage,
        )
    except CostPolicyError as exc:
        if "would exceed" in str(exc):
            raise CostPolicyError(
                "note indexing would exceed the configured cumulative cost ceiling"
            ) from exc
        raise
    selection = snapshot["stages"]["embed"]
    reservation = {
        "input_chars": input_chars,
        "requests": 1,
        "projected_usd": projected,
        "connection": selection.get("connection"),
        "model": selection.get("model"),
    }
    prior = list((attempt.usage or {}).get("reservations") or [])
    reservation_ids = list((attempt.usage or {}).get("dispatch_reservation_ids") or [])
    if reservation_id not in reservation_ids:
        reservation_ids.append(reservation_id)
    attempt.usage = {
        "reservations": prior + [reservation],
        "dispatch_reservation_ids": reservation_ids,
    }
    attempt.provider = (selection.get("connection") or "").split(":", 1)[-1] or None
    attempt.model = selection.get("model")
    reserved_cost = provider_cost_reservation_total(session, reservation_ids)
    return {
        "usage": usage,
        "pricing": pricing,
        "estimated_cost_usd": projected,
        "reserved_cost_usd": reserved_cost,
        "reservation_id": reservation_id,
    }


def _settle_document_dispatch_reservations(session, attempt) -> float:
    reservation_ids = list((attempt.usage or {}).get("dispatch_reservation_ids") or [])
    reserved_cost = provider_cost_reservation_total(session, reservation_ids)
    finalize_provider_cost_reservations(
        session,
        reservation_ids,
        status="completed",
        release=True,
    )
    return reserved_cost


def _validate_embedding_blobs(blobs: list[bytes], *, dim: int, expected_count: int) -> None:
    if dim <= 0 or len(blobs) != expected_count:
        raise ValueError("embedding provider returned an invalid vector shape")
    expected_bytes = dim * np.dtype(np.float32).itemsize
    for blob in blobs:
        if len(blob) != expected_bytes:
            raise ValueError("embedding vector byte length does not match its dimension")
        if not np.isfinite(np.frombuffer(blob, dtype=np.float32)).all():
            raise ValueError("embedding provider returned a non-finite vector")


def _embed_note_chunks(
    chunks: list[str], settings: Settings, snapshot: dict, claim: dict
) -> tuple[list[bytes], str, int, dict, dict]:
    from .pipeline import (
        _remote_json_input,
        _run_remote_stage,
        _settings_for_stage,
        _validate_remote_returned_model,
    )

    failures: list[dict] = []
    candidates = candidate_snapshots(snapshot, "embed")
    for position, candidate in enumerate(candidates):
        selection = candidate["stages"]["embed"]
        try:
            with session_scope() as session:
                # Match mutation/cleanup's budget -> source -> document lock order.
                lock_cost_budget(session, claim.get("file_id"))
                valid_claim = _revalidate_claim_for_dispatch(session, claim, settings)
                if valid_claim:
                    cost = _embedding_cost_guard(session, candidate, sum(map(len, chunks)), claim)
            if not valid_claim:
                raise RuntimeError("note index claim was superseded before provider dispatch")
            if selection.get("execution_target") == "remote_worker":
                transcript = {
                    "segments": [
                        {
                            "text": chunk,
                            "start": float(index),
                            "end": float(index + 1),
                            # Distinct labels preserve one remote chunk per local
                            # passage; speaker identity is never published for notes.
                            "speaker": f"NOTE_{index}",
                            "words": [],
                        }
                        for index, chunk in enumerate(chunks)
                    ],
                    "language": None,
                    "duration": float(len(chunks)),
                    "provider": "localplaud-note",
                    "model": None,
                    "has_speakers": False,
                }
                payload = _run_remote_stage(
                    f"knowledge-document-{claim['id']}",
                    candidate,
                    "embed",
                    [_remote_json_input("transcript", transcript)],
                )
                remote_chunks = payload.get("chunks") or []
                vectors = payload.get("vectors_base64") or []
                if len(remote_chunks) != len(chunks) or len(vectors) != len(chunks):
                    raise ValueError("remote embedding returned a mismatched note chunk count")
                if [item.get("text") for item in remote_chunks] != chunks:
                    raise ValueError("remote embedding changed note chunk text")
                try:
                    blobs = [base64.b64decode(value, validate=True) for value in vectors]
                    dim = int(payload.get("dim") or 0)
                except (TypeError, ValueError) as exc:
                    raise ValueError("remote embedding returned invalid vector metadata") from exc
                _validate_embedding_blobs(blobs, dim=dim, expected_count=len(chunks))
                returned_model = _validate_remote_returned_model(payload, candidate, "embed")
                return (
                    blobs,
                    returned_model,
                    dim,
                    candidate,
                    cost,
                )
            candidate_settings = _settings_for_stage(settings, candidate, "embed")
            embedder = build_embedder(candidate_settings.embeddings)
            vectors = embedder.embed(chunks)
            if len(vectors) != len(chunks):
                raise ValueError("embedding provider returned a mismatched vector count")
            blobs = [np.asarray(vector, dtype=np.float32).tobytes() for vector in vectors]
            dim = len(vectors[0]) if vectors else 0
            _validate_embedding_blobs(blobs, dim=dim, expected_count=len(chunks))
            return blobs, embedder.name, dim, candidate, cost
        except Exception as exc:  # noqa: BLE001 - explicit fallback contract
            retryable = is_retryable_fallback_error(exc)
            failures.append(
                {
                    "index": position,
                    "connection": selection.get("connection"),
                    "model": selection.get("model"),
                    "error": sanitize_error(exc, max_length=500),
                    "retryable": retryable,
                }
            )
            if not retryable or position + 1 >= len(candidates):
                raise
    raise RuntimeError(f"no note embedding candidate executed: {failures}")


def _publish_document(
    claim: dict,
    chunks: list[str],
    blobs: list[bytes],
    model_name: str,
    dim: int,
    profile: dict,
    cost: dict,
) -> bool:
    _validate_embedding_blobs(blobs, dim=dim, expected_count=len(chunks))
    with session_scope() as session:
        lock_cost_budget(session, claim.get("file_id"))
        document = _lock_document_source_then_document(session, claim["id"])
        now = datetime.now(UTC)
        if document is None:
            attempt = session.get(KnowledgeIndexAttempt, claim["attempt_id"])
            if attempt is not None:
                reserved_cost = _settle_document_dispatch_reservations(session, attempt)
                attempt.status = "skipped"
                attempt.error = "knowledge document was deleted before publish"
                attempt.estimated_cost_usd = max(
                    float(attempt.estimated_cost_usd or 0), reserved_cost
                )
                attempt.completed_at = datetime.now(UTC)
            return False
        if not (
            document.generation == claim["generation"]
            and document.content_sha256 == claim["content_sha256"]
            and document.lease_token == claim["lease_token"]
            and document.lease_until is not None
            and _as_utc(document.lease_until) > now
            and knowledge_document_is_current(
                session,
                document,
                allow_running_stage=True,
            )
        ):
            if document.lease_token == claim["lease_token"]:
                document.status = "pending"
                document.lease_token = None
                document.lease_until = None
            attempt = session.get(KnowledgeIndexAttempt, claim["attempt_id"])
            if attempt is not None:
                reserved_cost = _settle_document_dispatch_reservations(session, attempt)
                attempt.status = "skipped"
                attempt.error = "superseded by newer note inputs"
                attempt.estimated_cost_usd = max(
                    float(attempt.estimated_cost_usd or 0), reserved_cost
                )
                attempt.completed_at = now
            return False
        session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
        for idx, (chunk, blob) in enumerate(zip(chunks, blobs, strict=True)):
            session.add(
                KnowledgeChunk(
                    document_id=document.id,
                    idx=idx,
                    text=chunk,
                    embedding_model=model_name,
                    dim=dim,
                    embedding=blob,
                )
            )
        document.status = "completed"
        document.provider = profile["stages"]["embed"]["connection"].split(":", 1)[-1]
        document.model = model_name
        document.dim = dim
        document.profile_snapshot = profile
        document.indexed_at = datetime.now(UTC)
        document.lease_token = None
        document.lease_until = None
        document.next_retry_at = None
        document.error = None
        attempt = session.get(KnowledgeIndexAttempt, claim["attempt_id"])
        if attempt is not None:
            reserved_cost = _settle_document_dispatch_reservations(session, attempt)
            attempt.status = "completed"
            attempt.provider = document.provider
            attempt.model = model_name
            attempt.usage = dict(attempt.usage or {}) | {"actual": cost.get("usage") or {}}
            attempt.estimated_cost_usd = max(
                float(attempt.estimated_cost_usd or 0), reserved_cost
            )
            attempt.completed_at = datetime.now(UTC)
        return True


def _fail_document(claim: dict, exc: Exception) -> None:
    with session_scope() as session:
        lock_cost_budget(session, claim.get("file_id"))
        document = _lock_document(session, claim["id"])
        attempt = session.get(KnowledgeIndexAttempt, claim["attempt_id"])
        if attempt is not None and attempt.status == "running":
            reserved_cost = _settle_document_dispatch_reservations(session, attempt)
            attempt.status = "failed" if document is not None else "skipped"
            attempt.error = sanitize_error(exc, max_length=2000)
            attempt.estimated_cost_usd = max(
                float(attempt.estimated_cost_usd or 0), reserved_cost
            )
            attempt.completed_at = datetime.now(UTC)
        now = datetime.now(UTC)
        if (
            document is None
            or document.lease_token != claim["lease_token"]
            or document.lease_until is None
            or _as_utc(document.lease_until) <= now
        ):
            return
        if document.generation != claim["generation"]:
            document.status = "pending"
        else:
            document.status = "failed"
            document.error = sanitize_error(exc, max_length=2000)
            document.next_retry_at = now + timedelta(
                seconds=min(3600, 30 * (2 ** max(0, document.attempts - 1)))
            )
        document.lease_token = None
        document.lease_until = None


def index_document(document_id: int, settings: Settings | None = None) -> bool:
    """Index one durable note document without changing recording stage state."""
    settings = settings or get_settings()
    if not settings.pipeline.index:
        return False
    with _document_lock(document_id):
        claim = _claim_document(document_id, settings)
        if claim is None:
            return False
        try:
            chunks = build_note_chunks(claim["title"], claim["content"])
            if not chunks:
                raise ValueError("note has no indexable content")
            blobs, model_name, dim, profile, cost = _embed_note_chunks(
                chunks, settings, claim["snapshot"], claim
            )
            return _publish_document(claim, chunks, blobs, model_name, dim, profile, cost)
        except Exception as exc:  # noqa: BLE001 - note remains available
            _fail_document(claim, exc)
            log.exception("Note knowledge indexing failed for document %s", document_id)
            return False


def process_pending_documents(settings: Settings | None = None, *, limit: int = 20) -> int:
    settings = settings or get_settings()
    if not settings.pipeline.index:
        return 0
    now = datetime.now(UTC)
    with session_scope() as session:
        sync_knowledge_documents(session, settings)
        session.flush()
        ids = list(
            session.scalars(
                select(KnowledgeDocument.id)
                .where(
                    or_(
                        KnowledgeDocument.status == "pending",
                        KnowledgeDocument.status == "failed",
                        KnowledgeDocument.lease_until < now,
                    ),
                    or_(
                        KnowledgeDocument.next_retry_at.is_(None),
                        KnowledgeDocument.next_retry_at <= now,
                    ),
                )
                .order_by(KnowledgeDocument.updated_at, KnowledgeDocument.id)
                .limit(max(1, min(limit, 100)))
            )
        )
    return sum(index_document(document_id, settings) for document_id in ids)


def process_file_documents(
    file_id: str, settings: Settings | None = None, *, limit: int = 20
) -> int:
    """Index current pending note artifacts for one actively processed recording."""
    settings = settings or get_settings()
    if not settings.pipeline.index:
        return 0
    with session_scope() as session:
        sync_file_knowledge_documents(session, file_id, settings)
        session.flush()
        ids = list(
            session.scalars(
                select(KnowledgeDocument.id)
                .where(
                    KnowledgeDocument.file_id == file_id,
                    KnowledgeDocument.status.in_(("pending", "failed")),
                )
                .order_by(KnowledgeDocument.updated_at, KnowledgeDocument.id)
                .limit(max(1, min(limit, 100)))
            )
        )
    return sum(index_document(document_id, settings) for document_id in ids)
