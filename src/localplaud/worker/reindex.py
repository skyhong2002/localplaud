"""Re-index one recording without rerunning ASR.

Used after a transcript correction: the corrected canonical transcript (latest
revision) is re-chunked and re-embedded, and the durable ``index`` stage run
records the outcome. Summaries are deliberately not regenerated — that stays
an explicit user action.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select, text

from ..asr.base import Transcript
from ..config import Settings, get_settings
from ..db.models import (
    Chunk,
    PlaudFile,
    Speaker,
    StageAttempt,
    StageName,
    StageRun,
    StageStatus,
)
from ..db.session import session_scope
from ..providers.usage import (
    estimate_cost,
    normalize_usage,
    pricing_for_stage,
    process_peak_memory_mb,
)
from . import index
from .pipeline import (
    _PROFILE_SNAPSHOT,
    _begin_stage_in_session,
    _rehydrate_revision,
    _rehydrate_transcript,
    _select_raw_transcript,
    _set_stage_in_session,
)

log = logging.getLogger(__name__)

_locks_guard = threading.Lock()
_file_locks: dict[str, threading.Lock] = {}


def _file_lock(file_id: str) -> threading.Lock:
    with _locks_guard:
        return _file_locks.setdefault(file_id, threading.Lock())


@dataclass(frozen=True)
class _ReindexInput:
    transcript: Transcript
    source: str
    lineage: dict[str, object]
    speaker_names: dict[str, str]


def _load_reindex_input(session, file_id: str, settings: Settings) -> _ReindexInput | None:
    """Load canonical text and its identity from one database snapshot.

    Speaker fields deliberately retain recording-local stable keys. Editable display
    names are presentation data and are checked separately as part of the input
    generation fence.
    """
    row = session.get(PlaudFile, file_id)
    if row is None:
        return None
    raw = _select_raw_transcript(row, settings)
    if raw is None:
        return None
    revision = row.corrected_transcript_for_source(raw.source)
    if revision is not None:
        base = (
            session.get(type(raw), revision.base_transcript_id)
            if revision.base_transcript_id is not None
            else None
        )
        transcript = _rehydrate_revision(revision, base)
    else:
        transcript = _rehydrate_transcript(raw)
    names = dict(
        session.execute(
            select(Speaker.key, Speaker.display_name).where(
                Speaker.file_id == file_id,
                Speaker.display_name.is_not(None),
            )
        ).all()
    )
    return _ReindexInput(
        transcript=transcript,
        source=raw.source,
        lineage={
            "input_transcript_id": raw.id,
            "input_transcript_revision": revision.revision if revision else 0,
            "input_transcript_source": raw.source,
        },
        speaker_names=names,
    )


def _begin_reindex(
    file_id: str,
    settings: Settings,
    expected_revision: int | None,
    expected_speaker_names: dict[str, str] | None,
) -> tuple[_ReindexInput, str | None, int] | None:
    with session_scope() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        snapshot = _load_reindex_input(session, file_id, settings)
        if snapshot is None:
            return None
        if (
            expected_revision is not None
            and snapshot.lineage["input_transcript_revision"] != expected_revision
        ):
            return None
        if (
            expected_speaker_names is not None
            and snapshot.speaker_names != expected_speaker_names
        ):
            return None
        generation = _begin_stage_in_session(session, file_id, StageName.index)
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == file_id,
                StageRun.stage == StageName.index,
            )
        )
        assert run is not None
        return snapshot, generation, run.attempts


def _inputs_still_current(
    session,
    file_id: str,
    settings: Settings,
    snapshot: _ReindexInput,
    generation: str | None,
    attempt: int,
) -> bool:
    current = _load_reindex_input(session, file_id, settings)
    run = session.scalar(
        select(StageRun).where(
            StageRun.file_id == file_id,
            StageRun.stage == StageName.index,
        )
    )
    current_generation = (run.detail or {}).get("stale_generation") if run else None
    return bool(
        current is not None
        and run is not None
        and current.lineage == snapshot.lineage
        and current.speaker_names == snapshot.speaker_names
        and current_generation == generation
        and run.status == StageStatus.running
        and run.attempts == attempt
    )


def _skip_superseded_attempt(
    session,
    file_id: str,
    attempt: int,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    row = session.scalar(
        select(StageAttempt).where(
            StageAttempt.file_id == file_id,
            StageAttempt.stage == StageName.index,
            StageAttempt.attempt == attempt,
            StageAttempt.status == StageStatus.running,
        )
    )
    if row is not None:
        now = datetime.now(UTC)
        started = row.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        usage = normalize_usage(None)
        if (peak_memory := process_peak_memory_mb()) is not None:
            usage["process_peak_memory_mb"] = peak_memory
        snapshot = row.resolved_profile_snapshot
        pricing = pricing_for_stage(session, snapshot, "embed")
        row.status = StageStatus.skipped
        row.provider = provider or row.provider
        row.model = model or row.model
        row.error = "superseded by newer transcript inputs"
        row.usage = usage
        row.estimated_cost_usd = estimate_cost(usage, pricing)
        row.latency_ms = max(0, int((now - started).total_seconds() * 1000))
        row.completed_at = now


def _publish_reindex(
    file_id: str,
    settings: Settings,
    snapshot: _ReindexInput,
    generation: str | None,
    attempt: int,
    chunks: list[dict],
    blobs: list[bytes],
    model_name: str | None,
    dim: int,
) -> bool:
    with session_scope() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        if not _inputs_still_current(
            session, file_id, settings, snapshot, generation, attempt
        ):
            _skip_superseded_attempt(
                session,
                file_id,
                attempt,
                provider=settings.embeddings.provider,
                model=model_name,
            )
            return False
        session.execute(delete(Chunk).where(Chunk.file_id == file_id))
        for idx, (chunk, blob) in enumerate(zip(chunks, blobs, strict=True)):
            session.add(
                Chunk(
                    file_id=file_id,
                    idx=idx,
                    text=chunk["text"],
                    start=chunk["start"],
                    end=chunk["end"],
                    speaker=chunk["speaker"],
                    embedding_model=model_name,
                    dim=dim,
                    embedding=blob,
                    **snapshot.lineage,
                    resolved_profile_snapshot=_PROFILE_SNAPSHOT.get(),
                )
            )
        _set_stage_in_session(
            session,
            file_id,
            StageName.index,
            StageStatus.completed,
            provider=settings.embeddings.provider,
            model=model_name,
            artifact_source="local",
            detail={"transcript": snapshot.lineage},
            expected_stale_generation=generation,
        )
        return True


def _fail_reindex_if_current(
    file_id: str,
    settings: Settings,
    snapshot: _ReindexInput,
    generation: str | None,
    attempt: int,
    exc: Exception,
    model_name: str | None = None,
) -> bool:
    with session_scope() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        if not _inputs_still_current(
            session, file_id, settings, snapshot, generation, attempt
        ):
            _skip_superseded_attempt(
                session,
                file_id,
                attempt,
                provider=settings.embeddings.provider,
                model=model_name,
            )
            return False
        _set_stage_in_session(
            session,
            file_id,
            StageName.index,
            StageStatus.failed,
            error=str(exc),
            expected_stale_generation=generation,
        )
        return True


def reindex_file(
    file_id: str,
    settings: Settings | None = None,
    *,
    expected_revision: int | None = None,
    expected_speaker_names: dict[str, str] | None = None,
) -> bool:
    """Rebuild the embedding chunks for ``file_id`` from the canonical
    (corrected) transcript. Returns True on success; failures are recorded on
    the durable ``index`` stage run and never raise."""
    settings = settings or get_settings()
    with _file_lock(file_id):
        started = _begin_reindex(
            file_id,
            settings,
            expected_revision,
            expected_speaker_names,
        )
        if started is None:
            return False
        snapshot, generation, attempt = started
        model_name = None
        try:
            chunks = index.build_chunks(snapshot.transcript)
            if chunks:
                blobs, model_name, dim = index.embed_chunks(chunks, settings)
            else:
                blobs, model_name, dim = [], None, 0
            return _publish_reindex(
                file_id,
                settings,
                snapshot,
                generation,
                attempt,
                chunks,
                blobs,
                model_name,
                dim,
            )
        except Exception as exc:  # noqa: BLE001 - transcript/notes remain usable
            if _fail_reindex_if_current(
                file_id,
                settings,
                snapshot,
                generation,
                attempt,
                exc,
                model_name,
            ):
                log.exception("Re-indexing failed for %s", file_id)
            else:
                log.info("Discarded superseded re-index failure for %s", file_id)
            return False
