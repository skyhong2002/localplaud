"""Re-index one recording without rerunning ASR.

Used after a transcript correction: the corrected canonical transcript (latest
revision) is re-chunked and re-embedded, and the durable ``index`` stage run
records the outcome. Summaries are deliberately not regenerated — that stays
an explicit user action.
"""

from __future__ import annotations

import base64
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
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
from ..providers.fallback import candidate_snapshots, is_retryable_fallback_error
from ..providers.service import lock_library_profile_resolution, resolve_recording_profile
from ..providers.usage import (
    estimate_cost,
    finalize_provider_cost_reservations,
    lock_cost_budget,
    normalize_usage,
    pricing_for_stage,
    process_peak_memory_mb,
    provider_cost_reservation_total,
)
from . import index
from .claims import processing_claim
from .pipeline import (
    _PROFILE_SNAPSHOT,
    PipelineAlreadyRunning,
    _assert_processing_claim_in_session,
    _begin_stage_in_session,
    _claim_processing,
    _cost_guard,
    _profile_stage_matches,
    _rehydrate_revision,
    _rehydrate_transcript,
    _release_processing,
    _remote_json_input,
    _remote_selection,
    _renew_processing_claim,
    _run_remote_stage,
    _select_raw_transcript,
    _set_stage_in_session,
    _settings_for_stage,
    _transcript_payload,
    _validate_remote_returned_model,
    processing_claim_active,
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
        _assert_processing_claim_in_session(session, file_id)
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
        prior_run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == file_id,
                StageRun.stage == StageName.index,
            )
        )
        if prior_run is not None and prior_run.status == StageStatus.running:
            _skip_superseded_attempt(session, file_id, prior_run.attempts)
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
    profile_snapshot: dict,
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
        and _profile_stage_matches(
            profile_snapshot,
            resolve_recording_profile(session, file_id).to_dict(),
            "embed",
        )
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
        ).with_for_update()
    )
    if row is not None:
        dispatch_reservation_ids = list(
            (row.usage or {}).get("dispatch_reservation_ids") or []
        )
        reserved_cost = provider_cost_reservation_total(
            session, dispatch_reservation_ids
        )
        finalize_provider_cost_reservations(
            session,
            dispatch_reservation_ids,
            status="completed",
            release=True,
        )
        now = datetime.now(UTC)
        started = row.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        usage = normalize_usage(None)
        if dispatch_reservation_ids:
            usage["dispatch_reservation_ids"] = dispatch_reservation_ids
        if (peak_memory := process_peak_memory_mb()) is not None:
            usage["process_peak_memory_mb"] = peak_memory
        snapshot = row.resolved_profile_snapshot
        pricing = pricing_for_stage(session, snapshot, "embed")
        row.status = StageStatus.skipped
        row.provider = provider or row.provider
        row.model = model or row.model
        row.error = "superseded by newer transcript inputs"
        row.usage = usage
        row.estimated_cost_usd = max(
            float(row.estimated_cost_usd or 0),
            reserved_cost,
            estimate_cost(usage, pricing),
        )
        row.latency_ms = max(0, int((now - started).total_seconds() * 1000))
        row.completed_at = now


def _settle_displaced_attempt(
    file_id: str,
    attempt: int,
    *,
    provider: str | None,
    model: str | None,
) -> None:
    """Close spend for a late worker after a newer processing claim takes over."""
    with session_scope() as session:
        lock_cost_budget(session, file_id)
        _skip_superseded_attempt(
            session,
            file_id,
            attempt,
            provider=provider,
            model=model,
        )


def _lock_reindex_write_rows(session, file_id: str) -> None:
    """Use the global profile fence before recording-local PostgreSQL rows."""
    if session.get_bind().dialect.name != "postgresql":
        return
    lock_library_profile_resolution(session)
    session.scalar(
        select(PlaudFile.id).where(PlaudFile.id == file_id).with_for_update()
    )
    session.scalar(
        select(StageRun.id)
        .where(
            StageRun.file_id == file_id,
            StageRun.stage == StageName.index,
        )
        .with_for_update()
    )


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
    profile_snapshot: dict,
    provider: str,
    usage: dict,
) -> bool:
    with session_scope() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        _lock_reindex_write_rows(session, file_id)
        _assert_processing_claim_in_session(session, file_id)
        if not _inputs_still_current(
            session,
            file_id,
            settings,
            snapshot,
            generation,
            attempt,
            profile_snapshot,
        ):
            _skip_superseded_attempt(
                session,
                file_id,
                attempt,
                provider=provider,
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
                    resolved_profile_snapshot=profile_snapshot,
                )
            )
        _set_stage_in_session(
            session,
            file_id,
            StageName.index,
            StageStatus.completed,
            provider=provider,
            model=model_name,
            artifact_source="local",
            detail={"transcript": snapshot.lineage},
            usage=usage,
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
    profile_snapshot: dict | None = None,
    provider: str | None = None,
) -> bool:
    with session_scope() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        _lock_reindex_write_rows(session, file_id)
        _assert_processing_claim_in_session(session, file_id)
        if not _inputs_still_current(
            session,
            file_id,
            settings,
            snapshot,
            generation,
            attempt,
            profile_snapshot or {},
        ):
            _skip_superseded_attempt(
                session,
                file_id,
                attempt,
                provider=provider,
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


def _embed_reindex_chunks(
    file_id: str,
    transcript: Transcript,
    chunks: list[dict],
    settings: Settings,
    profile_snapshot: dict,
) -> tuple[list[bytes], str | None, int, str, dict]:
    selection = profile_snapshot["stages"]["embed"]
    provider = (selection.get("connection") or "").split(":", 1)[-1] or "unknown"
    usage = {
        "input_chars": len(transcript.text),
        "input_items": len(transcript.segments),
        "requests": 1,
    }
    _cost_guard(
        file_id,
        "embed",
        profile_snapshot,
        usage | {"projection": True},
    )
    if not chunks:
        return [], None, 0, provider, usage
    if _remote_selection(profile_snapshot, "embed"):
        payload = _run_remote_stage(
            file_id,
            profile_snapshot,
            "embed",
            [_remote_json_input("transcript", _transcript_payload(transcript))],
        )
        remote_chunks = payload.get("chunks") or []
        expected = [
            {
                key: chunk.get(key)
                for key in ("text", "start", "end", "speaker")
            }
            for chunk in chunks
        ]
        actual = [
            {key: chunk.get(key) for key in ("text", "start", "end", "speaker")}
            for chunk in remote_chunks
        ]
        if actual != expected:
            raise ValueError("remote embedding changed transcript chunk boundaries")
        try:
            blobs = [
                base64.b64decode(value, validate=True)
                for value in payload.get("vectors_base64") or []
            ]
            dim = int(payload.get("dim") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("remote embedding returned invalid vector metadata") from exc
        model_name = _validate_remote_returned_model(payload, profile_snapshot, "embed")
    else:
        candidate_settings = _settings_for_stage(settings, profile_snapshot, "embed")
        blobs, model_name, dim = index.embed_chunks(chunks, candidate_settings)
    if dim <= 0 or len(blobs) != len(chunks):
        raise ValueError("embedding provider returned an invalid vector shape")
    expected_bytes = dim * np.dtype(np.float32).itemsize
    if any(
        len(blob) != expected_bytes
        or not np.isfinite(np.frombuffer(blob, dtype=np.float32)).all()
        for blob in blobs
    ):
        raise ValueError("embedding provider returned invalid vector data")
    return blobs, model_name, dim, provider, usage


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
    with _file_lock(file_id):
        try:
            claim_token = _claim_processing(
                file_id,
                require_audio=False,
                mark_processing=False,
            )
        except PipelineAlreadyRunning:
            return False
        try:
            with processing_claim(file_id, claim_token):
                try:
                    return _reindex_file_claimed(
                        file_id,
                        settings or get_settings(),
                        expected_revision=expected_revision,
                        expected_speaker_names=expected_speaker_names,
                    )
                except PipelineAlreadyRunning:
                    return False
        finally:
            _release_processing(file_id, claim_token)


def _reindex_file_claimed(
    file_id: str,
    settings: Settings,
    *,
    expected_revision: int | None,
    expected_speaker_names: dict[str, str] | None,
) -> bool:
    with session_scope() as session:
        resolved = resolve_recording_profile(session, file_id).to_dict()
    candidates = candidate_snapshots(resolved, "embed")
    for position, candidate in enumerate(candidates):
        profile_token = _PROFILE_SNAPSHOT.set(candidate)
        started = _begin_reindex(
            file_id,
            settings,
            expected_revision,
            expected_speaker_names,
        )
        if started is None:
            _PROFILE_SNAPSHOT.reset(profile_token)
            return False
        snapshot, generation, attempt = started
        model_name = None
        provider = None
        try:
            chunks = index.build_chunks(snapshot.transcript)
            _renew_processing_claim(file_id)
            blobs, model_name, dim, provider, usage = _embed_reindex_chunks(
                file_id,
                snapshot.transcript,
                chunks,
                settings,
                candidate,
            )
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
                candidate,
                provider,
                usage,
            )
        except PipelineAlreadyRunning:
            _settle_displaced_attempt(
                file_id,
                attempt,
                provider=provider,
                model=model_name,
            )
            return False
        except Exception as exc:  # noqa: BLE001 - transcript remains usable
            current = _fail_reindex_if_current(
                file_id,
                settings,
                snapshot,
                generation,
                attempt,
                exc,
                model_name,
                candidate,
                provider,
            )
            retryable = is_retryable_fallback_error(exc)
            if not current:
                log.info("Discarded superseded re-index failure for %s", file_id)
                return False
            if not retryable or position + 1 >= len(candidates):
                log.exception("Re-indexing failed for %s", file_id)
                return False
        finally:
            _PROFILE_SNAPSHOT.reset(profile_token)
    return False


def process_pending_reindexes(
    settings: Settings | None = None, *, limit: int = 20
) -> int:
    """Run profile-change transcript reindexes from the durable stage queue."""
    settings = settings or get_settings()
    if not settings.pipeline.index:
        return 0
    with session_scope() as session:
        queued: list[str] = []
        for run in session.scalars(
            select(StageRun)
            .where(
                StageRun.stage == StageName.index,
                StageRun.status.in_((StageStatus.pending, StageStatus.running)),
            )
            .order_by(StageRun.updated_at, StageRun.id)
        ):
            if not bool((run.detail or {}).get("reindex_only")):
                continue
            recording = session.get(PlaudFile, run.file_id)
            if recording is None or processing_claim_active(recording):
                continue
            queued.append(run.file_id)
            if len(queued) >= max(1, min(limit, 100)):
                break
    return sum(reindex_file(file_id, settings) for file_id in queued)
