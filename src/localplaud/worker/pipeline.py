"""The worker pipeline — turn a downloaded recording into a knowledge entry.

Stages (each gated by config in ``pipeline``):
    convert -> transcribe -> diarize -> summarize -> mind_map -> index

State lives on the ``PlaudFile`` row; derived artifacts become ``Transcript``,
``Summary`` and ``Chunk`` rows. Stages are resumable: a file is only marked
``done`` when the enabled stages have all produced their artifacts.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import math
import os
import uuid
from contextvars import ContextVar
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, or_, select, update

from ..asr.base import Segment, Transcript, Word
from ..config import Settings, get_settings
from ..db.models import (
    Chunk,
    FileStatus,
    PlaudFile,
    ProviderConnection,
    StageAttempt,
    StageName,
    StageRun,
    StageStatus,
    TranscriptRevision,
)
from ..db.models import Summary as SummaryRow
from ..db.models import Transcript as TranscriptRow
from ..db.session import session_scope
from ..providers.fallback import candidate_snapshots, is_retryable_fallback_error
from ..providers.service import resolve_recording_profile
from ..providers.usage import (
    enforce_cost_ceiling,
    estimate_cost,
    normalize_usage,
    pricing_for_stage,
    process_peak_memory_mb,
)
from ..remote.client import RemoteWorkerClient
from ..remote.protocol import InputReference, JobStage, JobSubmitRequest
from ..store.files import wav_path
from ..store.speakers import (
    capture_speaker_evidence,
    reconcile_speaker_labels,
    speaker_keys_from_segments,
    sync_speakers,
)
from . import align, convert, index, mindmap, polish, summarize, summary_templates, transcribe
from .diarize import DiarizationUnavailable, diarize

log = logging.getLogger(__name__)
_PROFILE_SNAPSHOT: ContextVar[dict | None] = ContextVar("resolved_profile_snapshot", default=None)
_PROCESSING_LEASE = timedelta(hours=24)


class PipelineAlreadyRunning(RuntimeError):
    pass


def processing_claim_active(row: PlaudFile, *, now: datetime | None = None) -> bool:
    if not row.processing_token or row.processing_lease_until is None:
        return False
    lease = row.processing_lease_until
    if lease.tzinfo is None:
        lease = lease.replace(tzinfo=UTC)
    return lease > (now or datetime.now(UTC))


def _claim_processing(file_id: str) -> str:
    """Atomically claim one recording so UI and daemon work cannot overlap."""
    token = uuid.uuid4().hex
    now = datetime.now(UTC)
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise ValueError(f"unknown file {file_id}")
        if not row.audio_path or not Path(row.audio_path).exists():
            raise FileNotFoundError(f"audio missing for {file_id}: {row.audio_path}")
        claimed = session.execute(
            update(PlaudFile)
            .where(
                PlaudFile.id == file_id,
                or_(
                    PlaudFile.processing_token.is_(None),
                    PlaudFile.processing_lease_until.is_(None),
                    PlaudFile.processing_lease_until <= now,
                ),
            )
            .values(
                processing_token=token,
                processing_lease_until=now + _PROCESSING_LEASE,
                status=FileStatus.processing,
                error=None,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
    if claimed != 1:
        raise PipelineAlreadyRunning(f"recording {file_id} is already processing")
    return token


def _release_processing(file_id: str, token: str) -> None:
    with session_scope() as session:
        session.execute(
            update(PlaudFile)
            .where(PlaudFile.id == file_id, PlaudFile.processing_token == token)
            .values(processing_token=None, processing_lease_until=None)
            .execution_options(synchronize_session=False)
        )


def _settings_for_stage(settings: Settings, snapshot: dict, stage: str) -> Settings:
    """Project one resolved stage selection onto an isolated Settings copy."""
    selected = snapshot.get("stages", {}).get(stage)
    if not selected:
        return settings.model_copy(deep=True)

    resolved = settings.model_copy(deep=True)
    family = {
        "transcribe": "asr",
        "diarize": "diarize",
        "summarize": "llm",
        "mind_map": "llm",
        "embed": "embeddings",
        "ask": "llm",
        "correct": "llm",
    }.get(stage)
    if family is None:
        return resolved

    family_config = getattr(resolved, family)
    provider = selected.get("provider_type") or str(selected["connection"]).split(":", 1)[-1]
    family_config.provider = provider
    provider_config = getattr(family_config, provider.replace("-", "_"), None)
    for key, value in selected.get("configuration", {}).items():
        target = (
            provider_config
            if provider_config is not None and hasattr(provider_config, key)
            else family_config
        )
        if hasattr(target, key):
            setattr(target, key, value)
    if provider_config is not None and hasattr(provider_config, "model"):
        provider_config.model = selected.get("model")
    elif hasattr(family_config, "model"):
        family_config.model = selected.get("model")

    for key, value in selected.get("options", {}).items():
        target = (
            provider_config
            if provider_config is not None and hasattr(provider_config, key)
            else family_config
        )
        if hasattr(target, key):
            setattr(target, key, value)

    secret_ref = selected.get("secret_ref")
    if secret_ref:
        if not str(secret_ref).startswith("env:"):
            raise ValueError("unsupported secret reference; expected env:VARIABLE")
        secret = os.environ.get(str(secret_ref).removeprefix("env:"))
        if not secret:
            raise ValueError(f"provider secret is unavailable: {secret_ref}")
        if provider_config is not None and hasattr(provider_config, "api_key"):
            provider_config.api_key = secret

    if stage == "transcribe":
        # Provider-name fallback is intentionally disabled. Only the validated,
        # ordered stage selections in Profile fallback_policy may change provider.
        family_config.fallback = []
    return resolved


def _llm_model(settings: Settings) -> str | None:
    """Return the configured model for the selected LLM provider."""
    provider = settings.llm.provider.replace("-", "_")
    config = getattr(settings.llm, provider, None)
    return getattr(config, "model", None)


def _remote_selection(snapshot: dict, stage: str) -> dict | None:
    selection = snapshot.get("stages", {}).get(stage)
    return selection if selection and selection.get("execution_target") == "remote_worker" else None


def _remote_json_input(name: str, payload: dict) -> InputReference:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return InputReference(
        name=name,
        media_type="application/json",
        kind="inline_json",
        value=payload,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _remote_audio_input(path: Path) -> InputReference:
    data = path.read_bytes()
    return InputReference(
        name="audio",
        media_type="audio/wav",
        kind="inline_base64",
        value=base64.b64encode(data).decode(),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _transcript_payload(transcript: Transcript) -> dict:
    return {
        "segments": [asdict(segment) for segment in transcript.segments],
        "language": transcript.language,
        "duration": transcript.duration,
        "provider": transcript.provider,
        "model": transcript.model,
        "has_speakers": transcript.has_speakers,
    }


def _run_remote_stage(
    file_id: str,
    snapshot: dict,
    stage: str,
    inputs: list[InputReference],
    *,
    options: dict | None = None,
) -> dict:
    selection = _remote_selection(snapshot, stage)
    if selection is None:
        raise ValueError(f"stage is not remote: {stage}")
    with session_scope() as session:
        connection = session.scalar(
            select(ProviderConnection).where(ProviderConnection.key == selection["connection"])
        )
        if connection is None:
            raise ValueError(f"remote connection not found: {selection['connection']}")
        config = dict(connection.config or {})
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "file": file_id,
                "stage": stage,
                "selection": selection,
                "inputs": [i.sha256 for i in inputs],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    request = JobSubmitRequest(
        idempotency_key=fingerprint,
        stage=JobStage(stage),
        model=selection.get("model"),
        inputs=inputs,
        options=(selection.get("options") or {}) | (options or {}),
    )
    client = RemoteWorkerClient.from_config(config)
    try:
        result = client.submit_and_wait(request, timeout=float(config.get("job_timeout", 3600)))
    finally:
        client.close()
    artifact = result.artifacts.get("transcript.json") or result.artifacts.get("result.json")
    if artifact is None:
        raise ValueError(f"remote {stage} returned no JSON artifact")
    return json.loads(artifact)


def _set_stage(
    file_id: str,
    stage: StageName,
    status: StageStatus,
    *,
    begin_attempt: bool = False,
    provider: str | None = None,
    model: str | None = None,
    artifact_source: str | None = None,
    detail: dict | None = None,
    usage: dict | None = None,
    error: str | None = None,
) -> None:
    now = datetime.now(UTC)
    with session_scope() as session:
        run = session.scalar(
            select(StageRun).where(StageRun.file_id == file_id, StageRun.stage == stage)
        )
        if run is None:
            run = StageRun(file_id=file_id, stage=stage, attempts=0, detail={})
            session.add(run)
        snapshot = _PROFILE_SNAPSHOT.get()
        if snapshot is not None:
            run.resolved_profile_snapshot = snapshot
        profile_stage = "embed" if stage == StageName.index else stage.value
        if begin_attempt:
            run.attempts = (run.attempts or 0) + 1
            run.started_at = now
            run.completed_at = None
            selection = (snapshot or {}).get("stages", {}).get(profile_stage) or {}
            session.add(
                StageAttempt(
                    file_id=file_id,
                    stage=stage,
                    attempt=run.attempts,
                    status=StageStatus.running,
                    provider=(selection.get("connection") or "").split(":", 1)[-1] or None,
                    model=selection.get("model"),
                    resolved_profile_snapshot=snapshot,
                    started_at=now,
                )
            )
        run.status = status
        if provider is not None:
            run.provider = provider
        if model is not None:
            run.model = model
        if artifact_source is not None:
            run.artifact_source = artifact_source
        if detail is not None:
            run.detail = detail
        run.error = error[:2000] if error else None
        if status in {
            StageStatus.completed,
            StageStatus.degraded,
            StageStatus.failed,
            StageStatus.skipped,
        }:
            run.completed_at = now
            attempt = session.scalar(
                select(StageAttempt).where(
                    StageAttempt.file_id == file_id,
                    StageAttempt.stage == stage,
                    StageAttempt.attempt == run.attempts,
                    StageAttempt.status == StageStatus.running,
                )
            )
            if attempt is not None:
                started = attempt.started_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                latency_ms = max(0, int((now - started).total_seconds() * 1000))
                normalized = normalize_usage(usage)
                if (peak_memory := process_peak_memory_mb()) is not None:
                    normalized["process_peak_memory_mb"] = peak_memory
                pricing = pricing_for_stage(session, snapshot, profile_stage)
                cost = estimate_cost(normalized, pricing)
                attempt.status = status
                attempt.provider = run.provider or attempt.provider
                attempt.model = run.model or attempt.model
                attempt.usage = normalized
                attempt.estimated_cost_usd = cost
                attempt.latency_ms = latency_ms
                attempt.error = run.error
                attempt.completed_at = now
                run.detail = dict(run.detail or {}) | {
                    "usage": normalized,
                    "latency_ms": latency_ms,
                    "estimated_cost_usd": cost,
                    "pricing": pricing,
                }


def _begin_stage(file_id: str, stage: StageName) -> None:
    _set_stage(file_id, stage, StageStatus.running, begin_attempt=True)


def _finish_stage(file_id: str, stage: StageName, **metadata) -> None:
    _set_stage(file_id, stage, StageStatus.completed, **metadata)


def _skip_stage(file_id: str, stage: StageName, reason: str) -> None:
    _set_stage(file_id, stage, StageStatus.skipped, detail={"reason": reason})


def _fail_stage(file_id: str, stage: StageName, exc: Exception, *, degraded=False) -> None:
    _set_stage(
        file_id,
        stage,
        StageStatus.degraded if degraded else StageStatus.failed,
        error=str(exc),
    )


def _rehydrate_segments(segments: list[dict] | None) -> list[Segment]:
    segs = []
    for s in segments or []:
        words = [Word(**w) for w in s.get("words", [])]
        segs.append(
            Segment(
                text=s.get("text", ""),
                start=s.get("start", 0.0),
                end=s.get("end", 0.0),
                speaker=s.get("speaker"),
                words=words,
            )
        )
    return segs


def _rehydrate_transcript(row: TranscriptRow) -> Transcript:
    return Transcript(
        segments=_rehydrate_segments(row.segments),
        language=row.language,
        provider=row.provider,
        model=row.model,
        has_speakers=row.has_speakers,
    )


def _rehydrate_revision(rev: TranscriptRevision, base: TranscriptRow | None) -> Transcript:
    """Rehydrate a user-corrected revision, borrowing ASR provenance from the
    raw base transcript when it still exists."""
    return Transcript(
        segments=_rehydrate_segments(rev.segments),
        language=base.language if base is not None else None,
        provider=rev.provider or (base.provider if base is not None else "local-edit"),
        model=rev.model or (base.model if base is not None else None),
        has_speakers=rev.has_speakers,
    )


def _select_raw_transcript(row: PlaudFile, settings: Settings) -> TranscriptRow | None:
    """Select the raw transcript allowed by the configured artifact mode."""
    local = [item for item in row.transcripts if item.source == "local"]
    if settings.pipeline.artifact_mode == "independent":
        return local[-1] if local else None
    if settings.pipeline.prefer_cloud_artifacts:
        cloud = [item for item in row.transcripts if item.source in {"cloud", "plaud"}]
        return cloud[-1] if cloud else (local[-1] if local else None)
    return local[-1] if local else None


def _apply_speaker_display_names(transcript: Transcript, names: dict[str, str]) -> Transcript:
    """Use editable names in derived artifacts without mutating stored segments."""
    for segment in transcript.segments:
        if segment.speaker:
            segment.speaker = names.get(segment.speaker, segment.speaker)
        for word in segment.words:
            if word.speaker:
                word.speaker = names.get(word.speaker, word.speaker)
    return transcript


def _audio_seconds(row: PlaudFile, transcript: Transcript | None = None) -> float:
    if transcript is not None and transcript.duration is not None:
        return float(transcript.duration)
    return float(row.duration_ms or 0) / 1000


def _cost_guard(file_id: str, stage: str, snapshot: dict, usage: dict) -> dict:
    with session_scope() as session:
        return enforce_cost_ceiling(session, file_id, stage, snapshot, usage)


def _run_fallback_stage(
    file_id: str,
    profile_stage: str,
    durable_stage: StageName,
    snapshot: dict,
    operation,
):
    """Run primary then explicit retryable fallbacks as separate attempts."""
    candidates = candidate_snapshots(snapshot, profile_stage)
    failures: list[dict] = []
    for position, candidate in enumerate(candidates):
        token = _PROFILE_SNAPSHOT.set(candidate)
        _begin_stage(file_id, durable_stage)
        try:
            outcome = operation(candidate)
            detail = dict(outcome.get("detail") or {}) | {
                "fallback": candidate["fallback"],
                "fallback_failures": failures,
            }
            _finish_stage(
                file_id,
                durable_stage,
                provider=outcome.get("provider"),
                model=outcome.get("model"),
                artifact_source=outcome.get("artifact_source", "local"),
                detail=detail,
                usage=outcome.get("usage"),
            )
            return outcome.get("value"), candidate
        except Exception as exc:  # noqa: BLE001 - classified by provider contract
            retryable = is_retryable_fallback_error(exc)
            _fail_stage(file_id, durable_stage, exc)
            selection = candidate["stages"].get(profile_stage) or {}
            failures.append(
                {
                    "index": position,
                    "connection": selection.get("connection"),
                    "model": selection.get("model"),
                    "error": str(exc)[:500],
                    "retryable": retryable,
                }
            )
            if not retryable or position + 1 >= len(candidates):
                raise
        finally:
            _PROFILE_SNAPSHOT.reset(token)
    raise RuntimeError(f"no candidate executed for {profile_stage}")


def _llm_projected_usage(transcript: Transcript, settings: Settings) -> dict:
    chars = len(transcript.text)
    chunks = max(1, math.ceil(chars / settings.pipeline.summary_chunk_chars))
    reduce_calls = math.ceil(chunks / 8) if chunks > 1 else 0
    requests = chunks + reduce_calls + 1 if chunks > 1 else 1
    max_output_tokens = chunks * 1200 + reduce_calls * 1200 + 1500 if chunks > 1 else 1500
    return {
        "input_chars": math.ceil(chars * (1.5 if chunks > 1 else 1.0)),
        "output_tokens": max_output_tokens,
        "requests": requests,
        "projection": True,
    }


def reset_pipeline_retry(row: PlaudFile) -> None:
    """Reset consecutive retry state for a manual retry or a successful run."""
    row.pipeline_retry_count = 0
    row.pipeline_next_retry_at = None
    row.pipeline_last_failure_at = None


def _schedule_pipeline_retry(row: PlaudFile, settings: Settings) -> None:
    """Persist the next exponential-backoff deadline after a failed cycle."""
    now = datetime.now(UTC)
    row.pipeline_retry_count = (row.pipeline_retry_count or 0) + 1
    row.pipeline_last_failure_at = now
    maximum = settings.pipeline.retry_max_attempts
    if maximum <= 0 or row.pipeline_retry_count >= maximum:
        row.pipeline_next_retry_at = None
        return
    delay = min(
        settings.pipeline.retry_max_seconds,
        settings.pipeline.retry_base_seconds * (2 ** (row.pipeline_retry_count - 1)),
    )
    row.pipeline_next_retry_at = now + timedelta(seconds=delay)


def process_file(file_id: str, settings: Settings | None = None, force: bool = False) -> None:
    """Process one recording under an exclusive durable lease."""
    token = _claim_processing(file_id)
    try:
        _process_file_claimed(file_id, settings=settings, force=force)
    except Exception as exc:
        # The main pipeline records its own failures. This guard covers setup
        # errors (for example profile resolution) that occur before its try block.
        with session_scope() as session:
            row = session.get(PlaudFile, file_id)
            if row is not None and row.status == FileStatus.processing:
                row.status = FileStatus.error
                row.error = str(exc)[:2000]
                _schedule_pipeline_retry(row, settings or get_settings())
        raise
    finally:
        _release_processing(file_id, token)


def _process_file_claimed(
    file_id: str, settings: Settings | None = None, force: bool = False
) -> None:
    settings = settings or get_settings()
    pcfg = settings.pipeline

    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise ValueError(f"unknown file {file_id}")
        if not row.audio_path or not Path(row.audio_path).exists():
            raise FileNotFoundError(f"audio missing for {file_id}: {row.audio_path}")
        row.status = FileStatus.processing
        row.error = None
        audio = Path(row.audio_path)
        existing_wav = row.wav_path
        requested_template_key = row.note_template_key or pcfg.summary_template
        template_key = "default" if requested_template_key == "auto" else requested_template_key
        snapshot = resolve_recording_profile(session, file_id).to_dict()
        align_run = next(
            (run for run in row.stage_runs if run.stage == StageName.align), None
        )
        align_selection = (snapshot.get("stages") or {}).get("align") or {}
        previous_align_selection = (
            ((align_run.resolved_profile_snapshot or {}).get("stages") or {}).get("align") or {}
            if align_run is not None
            else {}
        )
        reusable_alignment = bool(
            not force
            and align_run is not None
            and align_run.status in {StageStatus.completed, StageStatus.degraded}
            and previous_align_selection == align_selection
            and (
                align_run.status == StageStatus.completed
                or not align.selection_uses_forced_alignment(align_selection)
            )
        )

    profile_token = _PROFILE_SNAPSHOT.set(snapshot)
    diarize_settings = _settings_for_stage(settings, snapshot, "diarize")
    summarize_settings = _settings_for_stage(settings, snapshot, "summarize")
    mind_map_settings = _settings_for_stage(settings, snapshot, "mind_map")
    summarize_settings.pipeline.summary_template = template_key
    mind_map_settings.pipeline.summary_template = template_key

    partial_errors: list[str] = []
    try:
        # --- convert (skip if the wav already exists) ----------------- #
        wav = Path(existing_wav) if existing_wav else wav_path(file_id)
        if pcfg.convert:
            if force or not wav.exists():
                _begin_stage(file_id, StageName.convert)
                try:
                    convert.to_wav(audio, wav)
                    with session_scope() as session:
                        session.get(PlaudFile, file_id).wav_path = str(wav)
                    _finish_stage(
                        file_id,
                        StageName.convert,
                        provider="ffmpeg",
                        artifact_source="local",
                        usage={"audio_seconds": _audio_seconds(row)},
                    )
                except Exception as exc:
                    _fail_stage(file_id, StageName.convert, exc)
                    raise
            else:
                _finish_stage(
                    file_id,
                    StageName.convert,
                    provider="ffmpeg",
                    artifact_source="local",
                    detail={"reused": True},
                )
        else:
            wav = audio
            _skip_stage(file_id, StageName.convert, "disabled")

        # --- transcribe (reuse an existing transcript to resume) ------ #
        transcript: Transcript | None = None
        transcript_source = "local"
        if pcfg.transcribe:
            existing = _load_transcript(file_id, settings) if not force else None
            if existing is not None:
                transcript, transcript_source = existing
                log.info("Reusing existing transcript for %s", file_id)
                _finish_stage(
                    file_id,
                    StageName.transcribe,
                    provider=transcript.provider,
                    model=transcript.model,
                    artifact_source=transcript_source,
                    detail={"reused": True},
                )
            else:

                def run_transcribe(candidate):
                    candidate_settings = _settings_for_stage(settings, candidate, "transcribe")
                    projected_usage = {"audio_seconds": _audio_seconds(row)}
                    cost_budget = _cost_guard(file_id, "transcribe", candidate, projected_usage)
                    if _remote_selection(candidate, "transcribe"):
                        payload = _run_remote_stage(
                            file_id, candidate, "transcribe", [_remote_audio_input(wav)]
                        )
                        result = Transcript(
                            segments=_rehydrate_segments(payload.get("segments")),
                            language=payload.get("language"),
                            duration=payload.get("duration"),
                            provider="remote-worker",
                            model=payload.get("model"),
                            has_speakers=payload.get("has_speakers", False),
                        )
                    else:
                        result = transcribe.run_asr(wav, candidate_settings)
                    _persist_transcript(file_id, result)
                    return {
                        "value": result,
                        "provider": result.provider,
                        "model": result.model,
                        "detail": {"cost_budget": cost_budget},
                        "usage": {
                            "audio_seconds": _audio_seconds(row, result),
                            "output_chars": len(result.text),
                        },
                    }

                transcript, _selected_snapshot = _run_fallback_stage(
                    file_id,
                    "transcribe",
                    StageName.transcribe,
                    snapshot,
                    run_transcribe,
                )
        else:
            _skip_stage(file_id, StageName.transcribe, "disabled")

        # --- align (provider timestamps or explicitly selected forced alignment) -- #
        if transcript is None:
            _skip_stage(file_id, StageName.align, "no transcript")
        elif not pcfg.align:
            _skip_stage(file_id, StageName.align, "disabled")
        elif transcript_source != "local":
            _skip_stage(file_id, StageName.align, "imported migration artifact")
        elif reusable_alignment:
            _finish_stage(
                file_id,
                StageName.align,
                provider=align_run.provider,
                model=align_run.model,
                artifact_source=transcript_source,
                detail=dict(align_run.detail or {}) | {"reused": True},
            )
        else:
            # Alignment is derived from the immutable ASR lane. A canonical user
            # or AI revision may have different text and must never be written
            # back into the raw transcript row.
            alignment_input = _load_raw_transcript(file_id, settings) or transcript

            def alignment_selection(candidate):
                selected = (candidate.get("stages") or {}).get("align")
                if selected:
                    return selected
                provider = transcript.provider or align.PROVIDER_TIMESTAMPS
                return {
                    "connection": f"asr:{provider}",
                    "provider_type": provider,
                    "model": transcript.model,
                    "options": {},
                }

            try:
                def run_align(candidate):
                    selection = alignment_selection(candidate)
                    if _remote_selection(candidate, "align"):
                        raise align.AlignmentUnavailable(
                            "remote-worker protocol v1 does not support alignment jobs"
                        )
                    provider = selection.get("provider_type") or str(
                        selection["connection"]
                    ).split(":", 1)[-1]
                    options = dict(selection.get("configuration") or {}) | dict(
                        selection.get("options") or {}
                    )
                    result = align.run_alignment(
                        wav,
                        alignment_input,
                        provider=provider,
                        model=selection.get("model"),
                        options=options,
                    )
                    if result.detail.get("forced_alignment"):
                        _persist_aligned_transcript(file_id, result.transcript)
                    return {
                        "value": result.transcript,
                        "provider": result.provider,
                        "model": result.model,
                        "artifact_source": transcript_source,
                        "detail": result.detail,
                        "usage": {"input_words": result.detail["word_count"]},
                    }

                transcript, _selected_snapshot = _run_fallback_stage(
                    file_id,
                    "align",
                    StageName.align,
                    snapshot,
                    run_align,
                )
            except align.AlignmentError as exc:
                with session_scope() as session:
                    failed_run = session.scalar(
                        select(StageRun).where(
                            StageRun.file_id == file_id,
                            StageRun.stage == StageName.align,
                        )
                    )
                    failed_snapshot = (
                        failed_run.resolved_profile_snapshot
                        if failed_run is not None
                        else snapshot
                    )
                failed_selection = alignment_selection(failed_snapshot)
                failed_provider = failed_selection.get("provider_type") or str(
                    failed_selection["connection"]
                ).split(":", 1)[-1]
                requested_forced = failed_provider == align.WHISPERX_PROVIDER
                if requested_forced or not isinstance(exc, align.AlignmentUnavailable):
                    partial_errors.append(f"align: {exc}")
                _set_stage(
                    file_id,
                    StageName.align,
                    StageStatus.degraded,
                    provider=failed_provider,
                    model=failed_selection.get("model"),
                    artifact_source=transcript_source,
                    detail={
                        "strategy": "unavailable",
                        "forced_alignment": False,
                        "requested_forced_alignment": requested_forced,
                        "retryable": isinstance(exc, align.AlignmentUnavailable),
                        "reason": str(exc),
                    },
                    error=str(exc),
                )

        # --- diarize (downstream/degradable; transcript stays usable) -- #
        if transcript is None:
            _skip_stage(file_id, StageName.diarize, "no transcript")
        elif not pcfg.diarize:
            _skip_stage(file_id, StageName.diarize, "disabled")
        elif transcript_source != "local":
            _skip_stage(file_id, StageName.diarize, "imported migration artifact")
        elif transcript.has_speakers:
            _finish_stage(
                file_id,
                StageName.diarize,
                provider=transcript.provider,
                model=transcript.model,
                artifact_source=transcript_source,
                detail={"reused": True, "provided_by_asr": True},
            )
        elif (
            diarize_settings.diarize.provider == "none"
            and len(candidate_snapshots(snapshot, "diarize")) == 1
        ):
            _skip_stage(file_id, StageName.diarize, "provider disabled")
        else:
            try:

                def run_diarize(candidate):
                    candidate_settings = _settings_for_stage(settings, candidate, "diarize")
                    source = copy.deepcopy(transcript)
                    if candidate_settings.diarize.provider == "none":
                        raise DiarizationUnavailable("diarization provider disabled")
                    projected_usage = {
                        "audio_seconds": _audio_seconds(row, source),
                        "input_chars": len(source.text),
                    }
                    cost_budget = _cost_guard(file_id, "diarize", candidate, projected_usage)
                    if _remote_selection(candidate, "diarize"):
                        payload = _run_remote_stage(
                            file_id,
                            candidate,
                            "diarize",
                            [
                                _remote_audio_input(wav),
                                _remote_json_input("transcript", _transcript_payload(source)),
                            ],
                        )
                        source.segments = _rehydrate_segments(payload.get("segments"))
                        source.has_speakers = payload.get("has_speakers", True)
                        provider = "remote-worker"
                    else:
                        source = diarize(wav, source, candidate_settings.diarize)
                        provider = candidate_settings.diarize.provider
                    # Legacy system-default snapshots can legitimately have no
                    # explicit stage map.  ``candidate_settings`` already
                    # resolves that case against durable settings.
                    model = candidate_settings.diarize.model
                    speaker_mapping = _persist_transcript(file_id, source)
                    return {
                        "value": source,
                        "provider": provider,
                        "model": model,
                        "detail": {
                            "cost_budget": cost_budget,
                            "speaker_reconciliation": speaker_mapping,
                        },
                        "usage": {
                            "audio_seconds": _audio_seconds(row, source),
                            "input_chars": len(source.text),
                            "output_chars": len(source.text),
                        },
                    }

                transcript, _selected_snapshot = _run_fallback_stage(
                    file_id, "diarize", StageName.diarize, snapshot, run_diarize
                )
            except DiarizationUnavailable as exc:
                log.warning("Diarization degraded for %s: %s", file_id, exc)
                _fail_stage(file_id, StageName.diarize, exc, degraded=True)
                partial_errors.append(f"diarize: {exc}")
            except Exception as exc:  # noqa: BLE001 - preserve usable transcript
                log.exception("Diarization failed for %s", file_id)
                partial_errors.append(f"diarize: {exc}")

        # Apply terminology only after ASR/diarization has produced its final raw
        # row. This creates a revision and never mutates provider output.
        from ..vocabulary import apply_vocabulary

        vocabulary_result = apply_vocabulary(file_id, automatic=True, settings=settings)
        if vocabulary_result.get("replacements"):
            with session_scope() as session:
                run = session.scalar(
                    select(StageRun).where(
                        StageRun.file_id == file_id,
                        StageRun.stage == StageName.transcribe,
                    )
                )
                if run is not None:
                    run.detail = dict(run.detail or {}) | {"vocabulary": vocabulary_result}

        # Plaud-style contextual cleanup: raw ASR remains immutable while the
        # polished text becomes the canonical revision consumed by notes/index.
        polish_failed = False
        polish_input = _load_transcript(file_id, settings)
        current_kind = None
        if polish_input is not None:
            transcript, transcript_source = polish_input
            with session_scope() as session:
                polish_row = session.get(PlaudFile, file_id)
                raw = _select_raw_transcript(polish_row, settings) if polish_row else None
                current = (
                    polish_row.corrected_transcript_for_source(raw.source)
                    if polish_row is not None and raw is not None
                    else None
                )
                current_kind = current.kind if current is not None else None
        if transcript is None:
            _skip_stage(file_id, StageName.correct, "no transcript")
        elif not pcfg.polish:
            _skip_stage(file_id, StageName.correct, "disabled")
        elif transcript_source != "local":
            _skip_stage(file_id, StageName.correct, "imported migration artifact")
        elif current_kind in {"user_edit", "restore"}:
            _skip_stage(file_id, StageName.correct, "preserved user correction")
        elif current_kind == "ai_polish" and not force:
            _finish_stage(
                file_id,
                StageName.correct,
                provider=transcript.provider,
                model=transcript.model,
                artifact_source="local",
                detail={"reused": True, "revision_kind": "ai_polish"},
            )
        else:
            try:

                def run_polish(candidate):
                    candidate_settings = _settings_for_stage(settings, candidate, "correct")
                    projected_usage = {
                        "input_chars": len(transcript.text),
                        "output_chars": len(transcript.text),
                        "projection": True,
                    }
                    cost_budget = _cost_guard(file_id, "correct", candidate, projected_usage)
                    result = polish.polish_transcript(transcript, candidate_settings)
                    revision = _persist_polished_revision(file_id, result, settings)
                    detail = dict(result.get("detail") or {}) | {
                        "revision": revision,
                        "prompt_version": result["prompt_version"],
                        "cost_budget": cost_budget,
                    }
                    return {
                        "value": result["transcript"],
                        "provider": result["provider"],
                        "model": result.get("model"),
                        "detail": detail,
                        "usage": {
                            "input_chars": detail.get("input_chars", len(transcript.text)),
                            "output_chars": detail.get("output_chars", 0),
                            "requests": detail.get("chunks", 1),
                        },
                    }

                transcript, _selected_snapshot = _run_fallback_stage(
                    file_id, "correct", StageName.correct, snapshot, run_polish
                )
            except Exception as exc:  # noqa: BLE001 - raw transcript remains usable
                log.exception("Transcript polish failed for %s", file_id)
                partial_errors.append(f"correct: {exc}")
                polish_failed = True

        # A force rebuild may replace the raw row while preserving user edits.
        # Reload the configured canonical lane before all derived stages so notes,
        # mind maps and the search index never drift from corrected transcript UI.
        canonical = _load_transcript(file_id, settings)
        transcript_lineage = _transcript_lineage(file_id, settings)
        if canonical is not None:
            transcript, transcript_source = canonical
        if polish_failed:
            transcript = None
        auto_recommendation = None
        if requested_template_key == "auto":
            from ..template_auto import recommend_template

            auto_recommendation = recommend_template(
                title=row.display_title,
                transcript=transcript.text if transcript is not None else "",
                duration_ms=row.duration_ms,
            )
            template_key = auto_recommendation["key"]
            summarize_settings.pipeline.summary_template = template_key
            mind_map_settings.pipeline.summary_template = template_key

        # --- summarize (skip if this template's summary already exists) #
        if pcfg.summarize and transcript is not None:
            if force or not _has_summary(file_id, template_key):
                try:

                    def run_summary(candidate):
                        candidate_settings = _settings_for_stage(settings, candidate, "summarize")
                        candidate_settings.pipeline.summary_template = template_key
                        projected_usage = _llm_projected_usage(transcript, candidate_settings)
                        cost_budget = _cost_guard(file_id, "summarize", candidate, projected_usage)
                        if _remote_selection(candidate, "summarize"):
                            result = _run_remote_stage(
                                file_id,
                                candidate,
                                "summarize",
                                [_remote_json_input("transcript", _transcript_payload(transcript))],
                                options={
                                    "template": summary_templates.template_snapshot(
                                        summary_templates.get_effective_template(template_key)
                                    )
                                },
                            )
                            result.setdefault("provider", "remote-worker")
                            result.setdefault("model", _llm_model(candidate_settings))
                        else:
                            result = summarize.summarize(transcript, candidate_settings)
                        _persist_summary(file_id, result, transcript_lineage)
                        return {
                            "value": result,
                            "provider": result.get("provider"),
                            "model": result.get("model"),
                            "detail": {
                                "template": result.get("template", "default"),
                                "coverage": result.get("coverage", {}),
                                "transcript": transcript_lineage,
                                "auto_template": auto_recommendation,
                                "cost_budget": cost_budget,
                            },
                            "usage": {
                                "input_chars": len(transcript.text),
                                "output_chars": len(result.get("content_md") or ""),
                                "requests": (
                                    (result.get("coverage") or {}).get("map_calls", 0)
                                    + (result.get("coverage") or {}).get("reduce_calls", 0)
                                    + 1
                                ),
                            },
                        }

                    _result, _selected_snapshot = _run_fallback_stage(
                        file_id,
                        "summarize",
                        StageName.summarize,
                        snapshot,
                        run_summary,
                    )
                except Exception as exc:  # noqa: BLE001 - transcript remains usable
                    log.exception("Summarization failed for %s", file_id)
                    partial_errors.append(f"summarize: {exc}")
            else:
                _finish_stage(
                    file_id,
                    StageName.summarize,
                    artifact_source="local",
                    detail={
                        "reused": True,
                        "template": template_key,
                        "auto_template": auto_recommendation,
                    },
                )
        else:
            _skip_stage(
                file_id,
                StageName.summarize,
                "disabled" if not pcfg.summarize else "no transcript",
            )

        # --- mind map (skip if a local mind map already exists) ------- #
        if pcfg.mind_map and transcript is not None:
            if force or not _has_summary(file_id, "mind_map"):
                try:

                    def run_mind_map(candidate):
                        candidate_settings = _settings_for_stage(settings, candidate, "mind_map")
                        candidate_settings.pipeline.summary_template = template_key
                        projected_usage = _llm_projected_usage(transcript, candidate_settings)
                        cost_budget = _cost_guard(file_id, "mind_map", candidate, projected_usage)
                        summary_md = _load_summary_md(file_id, template_key)
                        if _remote_selection(candidate, "mind_map"):
                            result = _run_remote_stage(
                                file_id,
                                candidate,
                                "mind_map",
                                [_remote_json_input("transcript", _transcript_payload(transcript))],
                                options={"summary_md": summary_md},
                            )
                            result.setdefault("provider", "remote-worker")
                            result.setdefault("model", _llm_model(candidate_settings))
                        else:
                            result = mindmap.generate_mind_map(
                                transcript, candidate_settings, summary_md
                            )
                        _persist_summary(file_id, result, transcript_lineage)
                        return {
                            "value": result,
                            "provider": result.get("provider"),
                            "model": result.get("model"),
                            "detail": (result.get("detail", {}))
                            | {
                                "transcript": transcript_lineage,
                                "auto_template": auto_recommendation,
                                "cost_budget": cost_budget,
                            },
                            "usage": {
                                "input_chars": len(transcript.text),
                                "output_chars": len(result.get("content_md") or ""),
                                "requests": (
                                    (result.get("detail") or {}).get("map_calls", 0)
                                    + (result.get("detail") or {}).get("reduce_calls", 0)
                                    + 1
                                ),
                            },
                        }

                    _result, _selected_snapshot = _run_fallback_stage(
                        file_id,
                        "mind_map",
                        StageName.mind_map,
                        snapshot,
                        run_mind_map,
                    )
                except Exception as exc:  # noqa: BLE001 - transcript/notes stay usable
                    log.exception("Mind map generation failed for %s", file_id)
                    partial_errors.append(f"mind_map: {exc}")
            else:
                _finish_stage(
                    file_id,
                    StageName.mind_map,
                    artifact_source="local",
                    detail={"reused": True},
                )
        else:
            _skip_stage(
                file_id,
                StageName.mind_map,
                "disabled" if not pcfg.mind_map else "no transcript",
            )

        # --- index (skip if chunks already exist) --------------------- #
        if pcfg.index and transcript is not None:
            if force or not _has_chunks(file_id):
                try:

                    def run_index(candidate):
                        candidate_settings = _settings_for_stage(settings, candidate, "embed")
                        projected_usage = {
                            "input_chars": len(transcript.text),
                            "input_items": len(transcript.segments),
                            "projection": True,
                        }
                        cost_budget = _cost_guard(file_id, "embed", candidate, projected_usage)
                        if _remote_selection(candidate, "embed"):
                            payload = _run_remote_stage(
                                file_id,
                                candidate,
                                "embed",
                                [_remote_json_input("transcript", _transcript_payload(transcript))],
                            )
                            model_name = _persist_remote_chunks(
                                file_id, payload, transcript_lineage
                            )
                            provider = "remote-worker"
                        else:
                            model_name = _persist_chunks(
                                file_id, transcript, candidate_settings, transcript_lineage
                            )
                            provider = candidate_settings.embeddings.provider
                        return {
                            "value": model_name,
                            "provider": provider,
                            "model": model_name,
                            "detail": {
                                "transcript": transcript_lineage,
                                "cost_budget": cost_budget,
                            },
                            "usage": {
                                "input_chars": len(transcript.text),
                                "input_items": len(transcript.segments),
                            },
                        }

                    _model, _selected_snapshot = _run_fallback_stage(
                        file_id, "embed", StageName.index, snapshot, run_index
                    )
                except Exception as exc:  # noqa: BLE001 - notes remain usable
                    log.exception("Indexing failed for %s", file_id)
                    partial_errors.append(f"index: {exc}")
            else:
                _finish_stage(
                    file_id,
                    StageName.index,
                    artifact_source="local",
                    detail={"reused": True},
                )
        else:
            _skip_stage(
                file_id,
                StageName.index,
                "disabled" if not pcfg.index else "no transcript",
            )

        with session_scope() as session:
            row = session.get(PlaudFile, file_id)
            if partial_errors:
                row.status = FileStatus.partial
                row.error = "; ".join(partial_errors)[:2000]
                _schedule_pipeline_retry(row, settings)
            else:
                row.status = FileStatus.done
                row.error = None
                reset_pipeline_retry(row)
        log.info("Pipeline %s for %s", "partial" if partial_errors else "complete", file_id)

    except Exception as exc:  # noqa: BLE001
        log.exception("Pipeline failed for %s", file_id)
        with session_scope() as session:
            r = session.get(PlaudFile, file_id)
            r.status = FileStatus.error
            r.error = str(exc)[:2000]
            _schedule_pipeline_retry(r, settings)
        raise
    finally:
        _PROFILE_SNAPSHOT.reset(profile_token)


def _load_transcript(file_id: str, settings: Settings) -> tuple[Transcript, str] | None:
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            return None
        selected = _select_raw_transcript(row, settings)
        selected_source = selected.source if selected is not None else "local"
        # Corrections only win in the same provenance lane selected by artifact
        # mode. A cloud-derived edit never satisfies independent mode.
        corrected = row.corrected_transcript_for_source(selected_source)
        if corrected is not None:
            base = (
                session.get(TranscriptRow, corrected.base_transcript_id)
                if corrected.base_transcript_id is not None
                else None
            )
            transcript = _rehydrate_revision(corrected, base)
            names = {
                speaker.key: speaker.display_name
                for speaker in row.speakers
                if speaker.display_name
            }
            return (_apply_speaker_display_names(transcript, names), corrected.source)
        if selected is None:
            return None
        transcript = _rehydrate_transcript(selected)
        names = {
            speaker.key: speaker.display_name for speaker in row.speakers if speaker.display_name
        }
        return (_apply_speaker_display_names(transcript, names), selected.source)


def _load_raw_transcript(file_id: str, settings: Settings) -> Transcript | None:
    """Load the immutable transcript lane without applying canonical revisions."""
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        selected = _select_raw_transcript(row, settings) if row is not None else None
        return _rehydrate_transcript(selected) if selected is not None else None


def _transcript_lineage(file_id: str, settings: Settings) -> dict | None:
    """Identify the exact canonical transcript input used by derived stages."""
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            return None
        raw = _select_raw_transcript(row, settings)
        if raw is None:
            return None
        revision = row.corrected_transcript_for_source(raw.source)
        return {
            "input_transcript_id": raw.id,
            "input_transcript_revision": revision.revision if revision else 0,
            "input_transcript_source": raw.source,
        }


def _has_summary(file_id: str, template: str) -> bool:
    expected_version = (
        None
        if template == "mind_map"
        else summary_templates.get_effective_template(template).version
    )
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        stage = StageName.mind_map if template == "mind_map" else StageName.summarize
        run = next((item for item in row.stage_runs if item.stage == stage), None)
        if run is not None and (run.detail or {}).get("stale"):
            return False
        return any(
            s.template == template
            and s.source == "local"
            and (template == "mind_map" or (s.template_version or 1) == expected_version)
            for s in row.summaries
        )


def _load_summary_md(file_id: str, template: str) -> str | None:
    with session_scope() as session:
        rows = [
            s.content_md
            for s in session.get(PlaudFile, file_id).summaries
            if s.template == template and s.source == "local"
        ]
        return rows[-1] if rows else None


def _has_chunks(file_id: str) -> bool:
    from ..db.models import Chunk

    with session_scope() as session:
        return session.query(Chunk.id).filter(Chunk.file_id == file_id).first() is not None


def _persist_transcript(file_id: str, transcript: Transcript) -> dict[str, str]:
    speaker_mapping: dict[str, str] = {}
    with session_scope() as session:
        # Preserve imported Plaud transcripts for comparison/migration. Only the
        # canonical local ASR result is replaced. User corrections
        # (TranscriptRevision rows) are never deleted here — edits survive
        # re-ASR; their base pointer is detached instead (SET NULL semantics,
        # applied explicitly because SQLite FK enforcement is off by default).
        replaced_ids = list(
            session.scalars(
                select(TranscriptRow.id).where(
                    TranscriptRow.file_id == file_id, TranscriptRow.source == "local"
                )
            )
        )
        previous_rows = list(
            session.scalars(
                select(TranscriptRow).where(
                    TranscriptRow.file_id == file_id, TranscriptRow.source == "local"
                )
            )
        )
        for previous in previous_rows:
            if previous.has_speakers:
                capture_speaker_evidence(session, file_id, previous.segments or [])
        if replaced_ids:
            session.execute(
                update(TranscriptRevision)
                .where(TranscriptRevision.base_transcript_id.in_(replaced_ids))
                .values(base_transcript_id=None)
            )
            session.execute(delete(TranscriptRow).where(TranscriptRow.id.in_(replaced_ids)))
        segments = [asdict(s) for s in transcript.segments]
        if transcript.has_speakers:
            speaker_mapping = reconcile_speaker_labels(session, file_id, segments)
        session.add(
            TranscriptRow(
                file_id=file_id,
                provider=transcript.provider,
                model=transcript.model,
                language=transcript.language,
                has_speakers=transcript.has_speakers,
                source="local",
                text=transcript.text,
                segments=segments,
                resolved_profile_snapshot=_PROFILE_SNAPSHOT.get(),
            )
        )
        # Register stable speaker identities; existing display names are kept.
        sync_speakers(session, file_id, speaker_keys_from_segments(segments))
    return speaker_mapping


def _persist_aligned_transcript(file_id: str, transcript: Transcript) -> None:
    """Update timing in place so forced alignment preserves ASR identity and edits."""
    with session_scope() as session:
        row = session.scalar(
            select(TranscriptRow)
            .where(TranscriptRow.file_id == file_id, TranscriptRow.source == "local")
            .order_by(TranscriptRow.id.desc())
        )
        if row is None:
            raise ValueError("forced alignment requires a persisted local transcript")
        if transcript.text != row.text:
            raise ValueError("forced alignment cannot replace immutable ASR text")
        segments = [asdict(segment) for segment in transcript.segments]
        row.segments = segments
        row.has_speakers = transcript.has_speakers
        if (snapshot := _PROFILE_SNAPSHOT.get()) is not None:
            row.resolved_profile_snapshot = snapshot
        sync_speakers(session, file_id, speaker_keys_from_segments(segments))


def _persist_polished_revision(file_id: str, result: dict, settings: Settings) -> int:
    from ..vocabulary import _mark_derived_stale

    transcript: Transcript = result["transcript"]
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise ValueError("recording not found")
        raw = _select_raw_transcript(row, settings)
        if raw is None or raw.source != "local":
            raise ValueError("AI polish requires a local raw transcript")
        next_revision = max(
            (item.revision for item in row.transcript_revisions), default=0
        ) + 1
        session.add(
            TranscriptRevision(
                file_id=file_id,
                base_transcript_id=raw.id,
                revision=next_revision,
                source="local",
                segments=[asdict(segment) for segment in transcript.segments],
                text=transcript.text,
                has_speakers=transcript.has_speakers,
                note=f"AI polished with {result['provider']}/{result.get('model') or 'default'}",
                kind="ai_polish",
                provider=result["provider"],
                model=result.get("model"),
                prompt_version=result["prompt_version"],
                resolved_profile_snapshot=_PROFILE_SNAPSHOT.get(),
            )
        )
        _mark_derived_stale(session, file_id, reason="ai_polish")
    return next_revision


def _persist_summary(file_id: str, result: dict, lineage: dict | None = None) -> None:
    template = result.get("template", "default")
    with session_scope() as session:
        session.execute(
            delete(SummaryRow).where(
                SummaryRow.file_id == file_id,
                SummaryRow.template == template,
                SummaryRow.source == "local",
            )
        )
        session.add(
            SummaryRow(
                file_id=file_id,
                template=template,
                template_version=result.get("template_version"),
                template_snapshot=result.get("template_snapshot"),
                title=result.get("title"),
                content_md=result.get("content_md", ""),
                llm_provider=result.get("provider"),
                model=result.get("model"),
                source="local",
                **(lineage or {}),
                resolved_profile_snapshot=_PROFILE_SNAPSHOT.get(),
            )
        )


def _persist_chunks(
    file_id: str,
    transcript: Transcript,
    settings: Settings,
    lineage: dict | None = None,
) -> str | None:
    chunks = index.build_chunks(transcript)
    if not chunks:
        return None
    blobs, model_name, dim = index.embed_chunks(chunks, settings)
    with session_scope() as session:
        session.execute(delete(Chunk).where(Chunk.file_id == file_id))
        for i, (c, blob) in enumerate(zip(chunks, blobs, strict=True)):
            session.add(
                Chunk(
                    file_id=file_id,
                    idx=i,
                    text=c["text"],
                    start=c["start"],
                    end=c["end"],
                    speaker=c["speaker"],
                    embedding_model=model_name,
                    dim=dim,
                    embedding=blob,
                    **(lineage or {}),
                    resolved_profile_snapshot=_PROFILE_SNAPSHOT.get(),
                )
            )
    return model_name


def _persist_remote_chunks(file_id: str, payload: dict, lineage: dict | None = None) -> str | None:
    chunks = payload.get("chunks", [])
    vectors = payload.get("vectors_base64", [])
    if len(chunks) != len(vectors):
        raise ValueError("remote embedding artifact has mismatched chunks and vectors")
    model_name = payload.get("model")
    dim = payload.get("dim")
    with session_scope() as session:
        session.execute(delete(Chunk).where(Chunk.file_id == file_id))
        for idx, (chunk, encoded) in enumerate(zip(chunks, vectors, strict=True)):
            session.add(
                Chunk(
                    file_id=file_id,
                    idx=idx,
                    text=chunk["text"],
                    start=chunk.get("start"),
                    end=chunk.get("end"),
                    speaker=chunk.get("speaker"),
                    embedding_model=model_name,
                    dim=dim,
                    embedding=base64.b64decode(encoded),
                    **(lineage or {}),
                    resolved_profile_snapshot=_PROFILE_SNAPSHOT.get(),
                )
            )
    return model_name


def process_pending(
    settings: Settings | None = None, limit: int | None = None, force: bool = False
) -> int:
    """Process fresh downloads first, then due failed/partial retries.

    Work runs up to ``pipeline.concurrency`` at a time. ``limit`` bounds a daemon
    batch; ``None`` drains all currently eligible work. Returns the count that
    reached either complete or usable-partial state.
    """
    settings = settings or get_settings()
    now = datetime.now(UTC)
    with session_scope() as session:
        due_retry = (
            PlaudFile.status.in_([FileStatus.error, FileStatus.partial])
            & PlaudFile.audio_path.is_not(None)
            & (PlaudFile.pipeline_retry_count < settings.pipeline.retry_max_attempts)
            & or_(
                PlaudFile.pipeline_next_retry_at.is_(None),
                PlaudFile.pipeline_next_retry_at <= now,
            )
        )
        rows = list(
            session.execute(
                select(
                    PlaudFile.id,
                    PlaudFile.status,
                    PlaudFile.start_time_ms,
                    PlaudFile.created_at,
                    PlaudFile.pipeline_next_retry_at,
                    PlaudFile.pipeline_last_failure_at,
                ).where(
                    PlaudFile.audio_path.is_not(None),
                    or_(PlaudFile.status == FileStatus.downloaded, due_retry),
                )
            )
        )

        def timestamp(value: datetime | None) -> float:
            if value is None:
                return 0.0
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.timestamp()

        def queue_key(item) -> tuple[float, int, str]:
            if item.status == FileStatus.downloaded:
                event_time = (
                    item.start_time_ms / 1000
                    if item.start_time_ms is not None
                    else timestamp(item.created_at)
                )
                fresh_tiebreak = 1
            else:
                event_time = timestamp(
                    item.pipeline_next_retry_at
                    or item.pipeline_last_failure_at
                    or item.created_at
                )
                fresh_tiebreak = 0
            return event_time, fresh_tiebreak, item.id

        rows.sort(key=queue_key, reverse=True)
        ids = [item.id for item in (rows[:limit] if limit is not None else rows)]
    if not ids:
        return 0

    workers = max(1, settings.pipeline.concurrency)

    def _run(fid: str) -> bool:
        try:
            process_file(fid, settings, force=force)
            return True
        except Exception:  # noqa: BLE001
            return False  # error already recorded on the row

    if workers == 1:
        return sum(_run(fid) for fid in ids)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return sum(pool.map(_run, ids))
