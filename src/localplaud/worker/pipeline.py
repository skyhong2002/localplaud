"""The worker pipeline — turn a downloaded recording into a knowledge entry.

Stages (each gated by config in ``pipeline``):
    convert -> transcribe -> diarize -> summarize -> mind_map -> index

State lives on the ``PlaudFile`` row; derived artifacts become ``Transcript``,
``Summary`` and ``Chunk`` rows. Stages are resumable: a file is only marked
``done`` when the enabled stages have all produced their artifacts.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, or_, select, update

from ..asr.base import Segment, Transcript, Word
from ..config import Settings, get_settings
from ..db.models import (
    Chunk,
    ExecutionProfile,
    FileStatus,
    NoteTemplate,
    PlaudFile,
    SpeakerTimeline,
    StageAttempt,
    StageName,
    StageRun,
    StageStatus,
    SummaryRevision,
    TranscriptRevision,
)
from ..db.models import Summary as SummaryRow
from ..db.models import Transcript as TranscriptRow
from ..db.session import session_scope
from ..providers.materialize import materialize_snapshot
from ..providers.resolver import resolve_profile
from ..providers.routing import settings_for_route
from ..store.files import wav_path
from ..store.speakers import reconcile_speaker_keys, speaker_keys_from_segments, sync_speakers
from . import align as alignment
from . import convert, correct, index, mindmap, summarize, transcribe
from .align import AlignmentUnavailable
from .diarize import DiarizationUnavailable, diarize

log = logging.getLogger(__name__)


def _speech_usage(transcript: Transcript) -> dict:
    duration = transcript.duration
    if duration is None and transcript.segments:
        duration = max(segment.end for segment in transcript.segments)
    return {"audio_seconds": duration,
            "segments": len(transcript.segments),
            "words": sum(len(segment.words) for segment in transcript.segments)}


def _ensure_profile_snapshot(session, file_id: str, stage: StageName, run: StageRun,
                             resolution_context: dict | None = None) -> dict:
    if run.profile_snapshot:
        return run.profile_snapshot
    file = session.get(PlaudFile, file_id)
    system_default = session.scalar(
        select(ExecutionProfile).where(ExecutionProfile.is_system_default)
    )
    if system_default is None:
        raise RuntimeError("no system-default execution profile; run localplaud init")
    override = (
        session.get(ExecutionProfile, file.execution_profile_id)
        if file is not None and file.execution_profile_id is not None else None
    )
    context = dict(file.profile_context or {}) if file is not None else {}
    context.update(resolution_context or {})
    folder_rule_id = context.get("rule_profile_id") or context.get("folder_profile_id")
    folder_rule = session.get(ExecutionProfile, folder_rule_id) if folder_rule_id else None
    template_profile_id = context.get("template_profile_id")
    if context.get("template_name"):
        template = session.scalar(select(NoteTemplate).where(
            NoteTemplate.name == context["template_name"], NoteTemplate.enabled,
        ))
        if template and template.execution_profile_id:
            template_profile_id = template.execution_profile_id
    template_profile = session.get(ExecutionProfile, template_profile_id) \
        if template_profile_id else None
    run.profile_snapshot = materialize_snapshot(session, resolve_profile(
        stage, system_default=system_default, folder_rule=folder_rule,
        template_default=template_profile, recording_override=override
    ))
    return run.profile_snapshot


def _profile_provider(file_id: str, stage: StageName) -> str:
    with session_scope() as session:
        run = session.scalar(select(StageRun).where(
            StageRun.file_id == file_id, StageRun.stage == stage
        ))
        if run is None:
            run = StageRun(file_id=file_id, stage=stage, attempts=0,
                           status=StageStatus.pending, detail={})
            session.add(run)
        elif (run.detail or {}).get("profile_change_pending"):
            preview = StageRun(file_id=file_id, stage=stage, detail={}, profile_snapshot={})
            return _ensure_profile_snapshot(session, file_id, stage, preview)["route"]["provider"]
        return _ensure_profile_snapshot(session, file_id, stage, run)["route"]["provider"]


def _retry_policy(file_id: str, settings: Settings) -> tuple[int, int, int]:
    values = (settings.pipeline.retry_max_attempts, settings.pipeline.retry_base_seconds,
              settings.pipeline.retry_max_seconds)
    with session_scope() as session:
        runs = list(session.scalars(select(StageRun).where(
            StageRun.file_id == file_id
        ).order_by(StageRun.updated_at.desc(), StageRun.id.desc())))
        policy = runs[0].profile_snapshot.get("policy", {}) if runs else {}
    retry = policy.get("retry", {})
    if not isinstance(retry, dict):
        return values
    return (int(retry.get("max_attempts", values[0])),
            int(retry.get("base_seconds", values[1])),
            int(retry.get("max_seconds", values[2])))


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
    error: str | None = None,
    resolution_context: dict | None = None,
    usage: dict | None = None,
    actual_cost: float | None = None,
) -> None:
    now = datetime.now(UTC)
    with session_scope() as session:
        run = session.scalar(
            select(StageRun).where(StageRun.file_id == file_id, StageRun.stage == stage)
        )
        if run is None:
            run = StageRun(file_id=file_id, stage=stage, attempts=0, detail={})
            session.add(run)
        if begin_attempt and (run.detail or {}).get("profile_change_pending"):
            run.profile_snapshot = {}
            run.detail = {key: value for key, value in (run.detail or {}).items()
                          if key != "profile_change_pending"}
        _ensure_profile_snapshot(session, file_id, stage, run, resolution_context)
        if begin_attempt:
            run.attempts = (run.attempts or 0) + 1
            run.started_at = now
            run.completed_at = None
            route = (run.profile_snapshot or {}).get("route", {})
            run.estimated_cost = route.get("estimated_cost")
            session.add(StageAttempt(
                file_id=file_id, stage=stage, attempt=run.attempts,
                status=StageStatus.running, provider=route.get("provider"),
                model=route.get("model"), profile_snapshot=run.profile_snapshot or {},
                started_at=now, estimated_cost=route.get("estimated_cost"),
            ))
        run.status = status
        if provider is not None:
            run.provider = provider
        if model is not None:
            run.model = model
        if artifact_source is not None:
            run.artifact_source = artifact_source
        if detail is not None:
            pending_profile = (run.detail or {}).get("profile_change_pending")
            run.detail = dict(detail)
            if pending_profile and not begin_attempt:
                run.detail["profile_change_pending"] = True
        run.error = error[:2000] if error else None
        if usage is not None:
            run.usage = usage
        if actual_cost is not None:
            run.actual_cost = actual_cost
        if status in {
            StageStatus.completed,
            StageStatus.degraded,
            StageStatus.failed,
            StageStatus.skipped,
        }:
            run.completed_at = now
            if run.started_at is not None:
                started_at = run.started_at
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=UTC)
                run.latency_ms = max(0, int((now - started_at).total_seconds() * 1000))
            attempt = session.scalar(select(StageAttempt).where(
                StageAttempt.file_id == file_id, StageAttempt.stage == stage,
                StageAttempt.attempt == run.attempts,
            ))
            if attempt is not None:
                attempt.status = status
                attempt.provider = provider if provider is not None else attempt.provider
                attempt.model = model if model is not None else attempt.model
                attempt.artifact_source = artifact_source
                attempt.detail = detail if detail is not None else attempt.detail
                attempt.error = run.error
                attempt.usage = run.usage or {}
                attempt.estimated_cost = run.estimated_cost
                attempt.actual_cost = run.actual_cost
                attempt.completed_at = now
                attempt.latency_ms = run.latency_ms


def _begin_stage(file_id: str, stage: StageName, resolution_context: dict | None = None) -> None:
    _set_stage(file_id, stage, StageStatus.running, begin_attempt=True,
               resolution_context=resolution_context)


def _routed_settings(file_id: str, stage: StageName, settings: Settings) -> Settings:
    with session_scope() as session:
        run = session.scalar(
            select(StageRun).where(StageRun.file_id == file_id, StageRun.stage == stage)
        )
        if run is None or not run.profile_snapshot:
            raise RuntimeError(f"stage {stage.value} has no resolved execution profile")
        snapshot = run.profile_snapshot
    return settings_for_route(settings, stage, snapshot)


def _run_with_fallback(file_id: str, stage: StageName, settings: Settings, operation):
    with session_scope() as session:
        run = session.scalar(select(StageRun).where(
            StageRun.file_id == file_id, StageRun.stage == stage
        ))
        if run is None or not run.profile_snapshot:
            raise RuntimeError(f"stage {stage.value} has no resolved execution profile")
        snapshot = deepcopy(run.profile_snapshot)
    routes = [snapshot["route"], *snapshot["route"].get("fallback", [])]
    errors = []
    for fallback_index, route in enumerate(routes):
        candidate = deepcopy(snapshot)
        candidate["route"] = deepcopy(route)
        candidate["route"]["fallback"] = []
        try:
            routed = settings_for_route(settings, stage, candidate)
            return operation(routed), routed, route, fallback_index
        except Exception as exc:  # noqa: BLE001 - explicit ordered fallback boundary
            errors.append(f"{route.get('provider')}: {exc}")
            if fallback_index + 1 < len(routes):
                log.warning("%s stage provider %s failed; trying explicit fallback: %s",
                            stage.value, route.get("provider"), exc)
                continue
            raise


def _actual_route_detail(file_id: str, stage: StageName, provider: str,
                         model: str | None) -> dict:
    with session_scope() as session:
        run = session.scalar(select(StageRun).where(
            StageRun.file_id == file_id, StageRun.stage == stage
        ))
        snapshot = run.profile_snapshot if run else {}
    primary = snapshot.get("route", {})
    routes = [primary, *primary.get("fallback", [])]
    index = next((idx for idx, route in enumerate(routes)
                  if route.get("provider") == provider
                  and (model is None or route.get("model") in {None, model})), None)
    route = routes[index] if index is not None else {}
    return {"selected_provider": primary.get("provider"), "actual_provider": provider,
            "actual_model": model, "fallback_used": bool(index),
            "fallback_index": index, "actual_connection": route.get("connection")}


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
        provider=base.provider if base is not None else "local-edit",
        model=base.model if base is not None else None,
        has_speakers=rev.has_speakers,
    )


def _select_raw_transcript(row: PlaudFile, settings: Settings) -> TranscriptRow | None:
    """Select the raw transcript allowed by the configured artifact mode."""
    local = row.local_transcript
    if settings.pipeline.artifact_mode == "independent":
        return local
    if settings.pipeline.prefer_cloud_artifacts:
        cloud = [item for item in row.transcripts if item.source in {"cloud", "plaud"}]
        return cloud[-1] if cloud else local
    return local


def _apply_speaker_display_names(
    transcript: Transcript, names: dict[str, str]
) -> Transcript:
    """Use editable names in derived artifacts without mutating stored segments."""
    for segment in transcript.segments:
        if segment.speaker:
            segment.speaker = names.get(segment.speaker, segment.speaker)
        for word in segment.words:
            if word.speaker:
                word.speaker = names.get(word.speaker, word.speaker)
    return transcript


class ProcessingAlreadyRunning(RuntimeError):
    """Raised when another process owns the recording's durable lease."""


def _claim_processing(file_id: str, settings: Settings) -> str:
    from datetime import timedelta
    from uuid import uuid4

    now = datetime.now(UTC)
    token = uuid4().hex
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
                (PlaudFile.status != FileStatus.processing)
                | (PlaudFile.processing_lease_until.is_(None))
                | (PlaudFile.processing_lease_until <= now),
            )
            .values(
                status=FileStatus.processing,
                error=None,
                processing_token=token,
                processing_lease_until=now + timedelta(
                    seconds=settings.pipeline.processing_lease_seconds
                ),
            )
            .execution_options(synchronize_session=False)
        ).rowcount
    if claimed != 1:
        raise ProcessingAlreadyRunning(f"recording {file_id} is already processing")
    return token


def process_file(
    file_id: str,
    settings: Settings | None = None,
    force: bool = False,
    *,
    claim_token: str | None = None,
) -> None:
    settings = settings or get_settings()
    pcfg = settings.pipeline

    if claim_token is None:
        claim_token = _claim_processing(file_id, settings)

    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise ValueError(f"unknown file {file_id}")
        if not row.audio_path or not Path(row.audio_path).exists():
            raise FileNotFoundError(f"audio missing for {file_id}: {row.audio_path}")
        if row.processing_token != claim_token or row.status != FileStatus.processing:
            raise ProcessingAlreadyRunning(f"recording {file_id} processing lease was lost")
        audio = Path(row.audio_path)
        existing_wav = row.wav_path

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
                _begin_stage(file_id, StageName.transcribe)
                try:
                    transcript = transcribe.run_asr(
                        wav, _routed_settings(file_id, StageName.transcribe, settings)
                    )
                    transcript = _persist_transcript(
                        file_id, transcript, artifact_kind="raw_asr",
                        provenance_stage=StageName.transcribe,
                    )
                    _finish_stage(
                        file_id,
                        StageName.transcribe,
                        provider=transcript.provider,
                        model=transcript.model,
                        artifact_source="local",
                        detail=_actual_route_detail(
                            file_id, StageName.transcribe, transcript.provider, transcript.model
                        ),
                        usage=_speech_usage(transcript),
                    )
                except Exception as exc:
                    _fail_stage(file_id, StageName.transcribe, exc)
                    raise
        else:
            _skip_stage(file_id, StageName.transcribe, "disabled")

        # --- align (word timestamps are a visible quality contract) --- #
        if transcript is None:
            _skip_stage(file_id, StageName.align, "no transcript")
        elif not pcfg.align:
            _skip_stage(file_id, StageName.align, "disabled")
        elif transcript_source != "local":
            _skip_stage(file_id, StageName.align, "imported migration artifact")
        elif _profile_provider(file_id, StageName.align) == "none":
            _skip_stage(file_id, StageName.align, "provider disabled")
        elif not force and _stage_is_fresh(file_id, StageName.align):
            _finish_stage(file_id, StageName.align, artifact_source="local",
                          detail={"reused": True})
        else:
            _begin_stage(file_id, StageName.align)
            try:
                ((transcript, align_detail), align_settings, align_route,
                 align_fallback) = _run_with_fallback(
                    file_id, StageName.align, settings,
                    lambda routed: alignment.align(
                        wav, transcript, routed.align, routed.remote_input
                    ),
                )
                transcript = _persist_transcript(
                    file_id, transcript, artifact_kind="aligned",
                    provenance_stage=StageName.align,
                )
                align_detail = dict(align_detail) | {
                    "fallback_index": align_fallback,
                    "selected_connection": align_route.get("connection"),
                }
                _finish_stage(file_id, StageName.align,
                              provider=align_settings.align.provider,
                              model=align_settings.align.model,
                              artifact_source="local", detail=align_detail,
                              usage=_speech_usage(transcript))
            except AlignmentUnavailable as exc:
                log.warning("Alignment degraded for %s: %s", file_id, exc)
                _fail_stage(file_id, StageName.align, exc, degraded=True)
                partial_errors.append(f"align: {exc}")
            except Exception as exc:  # noqa: BLE001
                log.exception("Alignment failed for %s", file_id)
                _fail_stage(file_id, StageName.align, exc)
                partial_errors.append(f"align: {exc}")

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
        elif _profile_provider(file_id, StageName.diarize) == "none":
            _skip_stage(file_id, StageName.diarize, "provider disabled")
        else:
            _begin_stage(file_id, StageName.diarize)
            try:
                (transcript, diarize_settings, diarize_route,
                 diarize_fallback) = _run_with_fallback(
                    file_id, StageName.diarize, settings,
                    lambda routed: diarize(
                        wav, transcript, routed.diarize, routed.remote_input
                    ),
                )
                transcript = _persist_transcript(
                    file_id, transcript, artifact_kind="diarized",
                    provenance_stage=StageName.diarize,
                )
                _finish_stage(
                    file_id,
                    StageName.diarize,
                    provider=diarize_settings.diarize.provider,
                    model=diarize_settings.diarize.model,
                    artifact_source="local",
                    detail={"fallback_index": diarize_fallback,
                            "selected_connection": diarize_route.get("connection")},
                    usage=_speech_usage(transcript),
                )
            except DiarizationUnavailable as exc:
                log.warning("Diarization degraded for %s: %s", file_id, exc)
                _fail_stage(file_id, StageName.diarize, exc, degraded=True)
                partial_errors.append(f"diarize: {exc}")
            except Exception as exc:  # noqa: BLE001 - preserve usable transcript
                log.exception("Diarization failed for %s", file_id)
                _fail_stage(file_id, StageName.diarize, exc)
                partial_errors.append(f"diarize: {exc}")

        # --- correction (durable vocabulary revision; raw ASR is immutable) #
        if transcript is None:
            _skip_stage(file_id, StageName.correct, "no transcript")
        elif not pcfg.correct:
            _skip_stage(file_id, StageName.correct, "disabled")
        elif transcript_source != "local":
            _skip_stage(file_id, StageName.correct, "imported migration artifact")
        elif not force and _stage_is_fresh(file_id, StageName.correct):
            _finish_stage(file_id, StageName.correct, artifact_source="local",
                          detail={"reused": True})
        else:
            _begin_stage(file_id, StageName.correct)
            try:
                # Validate that the resolved route is an allowed local correction
                # capability even though the rules operate directly on transcript data.
                ((transcript, correction_detail), _,
                 correction_route, correction_fallback) = _run_with_fallback(
                    file_id, StageName.correct, settings,
                    lambda routed: (
                        correct.run_remote(file_id, transcript, routed)
                        if routed.llm.provider == "remote-worker"
                        else (correct.run(file_id, transcript)
                              if routed.llm.correction_provider == "local-rules"
                              else correct.run_llm(file_id, transcript, routed))
                    ),
                )
                correction_detail = dict(correction_detail) | {
                    "fallback_index": correction_fallback,
                    "selected_connection": correction_route.get("connection"),
                }
                correction_provider = correction_route["provider"]
                _finish_stage(file_id, StageName.correct, provider=correction_provider,
                              model=correction_route.get("model"), artifact_source="local",
                              detail=correction_detail,
                              usage=_speech_usage(transcript))
            except Exception as exc:  # noqa: BLE001 - preserve usable raw transcript
                log.exception("Vocabulary correction failed for %s", file_id)
                _fail_stage(file_id, StageName.correct, exc)
                partial_errors.append(f"correct: {exc}")

        # A force rebuild may replace the raw row while preserving user edits.
        # Reload the configured canonical lane before all derived stages so notes,
        # mind maps and the search index never drift from corrected transcript UI.
        canonical = _load_transcript(file_id, settings)
        if canonical is not None:
            transcript, transcript_source = canonical
        note_template = (
            summarize.auto_select_template(transcript)
            if transcript is not None and pcfg.summary_template == "auto"
            else pcfg.summary_template
        )

        # --- summarize (skip if this template's summary already exists) #
        if pcfg.summarize and transcript is not None:
            if force or not _has_summary(file_id, note_template):
                _begin_stage(file_id, StageName.summarize,
                             {"template_name": note_template})
                try:
                    (result, _, summary_route,
                     summary_fallback) = _run_with_fallback(
                        file_id, StageName.summarize, settings,
                        lambda routed: summarize.summarize(transcript, routed),
                    )
                    _persist_summary(file_id, result)
                    _finish_stage(
                        file_id,
                        StageName.summarize,
                        provider=result.get("provider"),
                        model=result.get("model"),
                        artifact_source="local",
                        detail={
                            "template": result.get("template", "default"),
                            "coverage": result.get("coverage", {}),
                            "fallback_index": summary_fallback,
                            "selected_connection": summary_route.get("connection"),
                        },
                        usage=(result.get("usage") or {
                            "input_chars": result.get("coverage", {}).get("transcript_chars"),
                            "output_chars": len(result.get("content_md", "")),
                            "provider_reported_tokens": False,
                        }),
                    )
                except Exception as exc:  # noqa: BLE001 - transcript remains usable
                    log.exception("Summarization failed for %s", file_id)
                    _fail_stage(file_id, StageName.summarize, exc)
                    partial_errors.append(f"summarize: {exc}")
            else:
                _finish_stage(
                    file_id,
                    StageName.summarize,
                    artifact_source="local",
                    detail={"reused": True, "template": note_template},
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
                _begin_stage(file_id, StageName.mind_map,
                             {"template_name": note_template})
                try:
                    summary_md = _load_summary_md(file_id, note_template)
                    (result, _, mind_route,
                     mind_fallback) = _run_with_fallback(
                        file_id, StageName.mind_map, settings,
                        lambda routed: mindmap.generate_mind_map(
                            transcript, routed, summary_md
                        ),
                    )
                    _persist_summary(file_id, result)
                    _finish_stage(
                        file_id,
                        StageName.mind_map,
                        provider=result.get("provider"),
                        model=result.get("model"),
                        artifact_source="local",
                        detail=(result.get("detail", {}) | {
                            "fallback_index": mind_fallback,
                            "selected_connection": mind_route.get("connection"),
                        }),
                        usage=(result.get("usage") or {
                            "output_chars": len(result.get("content_md", "")),
                            "provider_reported_tokens": False,
                        }),
                    )
                except Exception as exc:  # noqa: BLE001 - transcript/notes stay usable
                    log.exception("Mind map generation failed for %s", file_id)
                    _fail_stage(file_id, StageName.mind_map, exc)
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
                _begin_stage(file_id, StageName.index)
                try:
                    ((model_name, actual_embedding_provider), _,
                     index_route, index_fallback) = _run_with_fallback(
                        file_id, StageName.index, settings,
                        lambda routed: (
                            _persist_chunks(file_id, transcript, routed),
                            routed.embeddings.provider,
                        ),
                    )
                    _finish_stage(
                        file_id,
                        StageName.index,
                        provider=actual_embedding_provider,
                        model=model_name,
                        artifact_source="local",
                        detail={"fallback_index": index_fallback,
                                "selected_connection": index_route.get("connection")},
                        usage={"chunks": _chunk_count(file_id)},
                    )
                except Exception as exc:  # noqa: BLE001 - notes remain usable
                    log.exception("Indexing failed for %s", file_id)
                    _fail_stage(file_id, StageName.index, exc)
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
            finished = session.execute(
                update(PlaudFile)
                .where(PlaudFile.id == file_id, PlaudFile.processing_token == claim_token)
                .values(
                    status=FileStatus.partial if partial_errors else FileStatus.done,
                    error="; ".join(partial_errors)[:2000] if partial_errors else None,
                    retry_count=0, retry_after=None, processing_token=None,
                    processing_lease_until=None,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if finished != 1:
                raise ProcessingAlreadyRunning(
                    f"recording {file_id} processing lease was lost before completion"
                )
        log.info("Pipeline %s for %s", "partial" if partial_errors else "complete", file_id)

    except Exception as exc:  # noqa: BLE001
        log.exception("Pipeline failed for %s", file_id)
        with session_scope() as session:
            r = session.get(PlaudFile, file_id)
            if r.processing_token != claim_token:
                raise
            r.status = FileStatus.error
            r.error = str(exc)[:2000]
            r.retry_count = (r.retry_count or 0) + 1
            max_attempts, base_seconds, max_seconds = _retry_policy(file_id, settings)
            if r.retry_count < max_attempts:
                delay = min(max_seconds, base_seconds * (2 ** (r.retry_count - 1)))
                from datetime import timedelta

                r.retry_after = datetime.now(UTC) + timedelta(seconds=delay)
            else:
                r.retry_after = None
            r.processing_token = None
            r.processing_lease_until = None
        raise


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
            speaker.key: speaker.display_name
            for speaker in row.speakers
            if speaker.display_name
        }
        return (_apply_speaker_display_names(transcript, names), selected.source)


def _has_summary(file_id: str, template: str) -> bool:
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        stage = StageName.mind_map if template == "mind_map" else StageName.summarize
        run = next((item for item in row.stage_runs if item.stage == stage), None)
        if run is not None and (run.detail or {}).get("stale"):
            return False
        return any(
            s.template == template and s.source == "local"
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


def _chunk_count(file_id: str) -> int:
    with session_scope() as session:
        return session.query(Chunk.id).filter(Chunk.file_id == file_id).count()


def _stage_is_fresh(file_id: str, stage: StageName) -> bool:
    with session_scope() as session:
        run = session.scalar(select(StageRun).where(
            StageRun.file_id == file_id, StageRun.stage == stage
        ))
        return bool(run and run.status == StageStatus.completed and not (run.detail or {}).get("stale"))


def _persist_transcript(
    file_id: str,
    transcript: Transcript,
    *,
    artifact_kind: str = "raw_asr",
    provenance_stage: StageName = StageName.transcribe,
) -> Transcript:
    with session_scope() as session:
        # Preserve imported Plaud transcripts for comparison/migration. Only the
        # canonical local ASR result is replaced. User corrections
        # (TranscriptRevision rows) are never deleted here — edits survive
        # re-ASR; their base pointer is detached instead (SET NULL semantics,
        # applied explicitly because SQLite FK enforcement is off by default).
        previous = session.scalars(
            select(TranscriptRow).where(
                TranscriptRow.file_id == file_id, TranscriptRow.source == "local"
            ).order_by(TranscriptRow.id)
        ).all()
        timeline = session.get(SpeakerTimeline, file_id)
        previous_speaker_segments = next(
            (row.segments for row in reversed(previous) if row.has_speakers),
            timeline.segments if timeline is not None else [],
        )
        if previous_speaker_segments and timeline is None:
            timeline = SpeakerTimeline(file_id=file_id, segments=[])
            session.add(timeline)
        if previous and previous[-1].has_speakers and timeline is not None:
            timeline.segments = [
                {"start": row.get("start", 0), "end": row.get("end", 0),
                 "speaker": row.get("speaker"), "words": []}
                for row in previous[-1].segments if row.get("speaker")
            ]
        segments = [asdict(s) for s in transcript.segments]
        if previous_speaker_segments and transcript.has_speakers:
            segments, _ = reconcile_speaker_keys(previous_speaker_segments, segments)
            transcript = _rehydrate_transcript(TranscriptRow(
                provider=transcript.provider, model=transcript.model,
                language=transcript.language, has_speakers=transcript.has_speakers,
                source="local", text=transcript.text, segments=segments,
            ))
        if transcript.has_speakers:
            if timeline is None:
                timeline = SpeakerTimeline(file_id=file_id, segments=[])
                session.add(timeline)
            timeline.segments = [
                {"start": row.get("start", 0), "end": row.get("end", 0),
                 "speaker": row.get("speaker"), "words": []}
                for row in segments if row.get("speaker")
            ]
        provenance_run = session.scalar(select(StageRun).where(
            StageRun.file_id == file_id, StageRun.stage == provenance_stage,
        ))
        # A fresh ASR invalidates all prior local speech derivatives. Later
        # stages replace only themselves/downstream, retaining raw_asr.
        invalidated_kinds = {
            "raw_asr": {"raw_asr", "aligned", "diarized", "canonical"},
            "aligned": {"aligned", "diarized", "canonical"},
            "diarized": {"diarized", "canonical"},
        }.get(artifact_kind, {artifact_kind})
        replaced_ids = list(
            session.scalars(
                select(TranscriptRow.id).where(
                    TranscriptRow.file_id == file_id,
                    TranscriptRow.source == "local",
                    TranscriptRow.artifact_kind.in_(invalidated_kinds),
                )
            )
        )
        if replaced_ids:
            session.execute(
                update(TranscriptRevision)
                .where(TranscriptRevision.base_transcript_id.in_(replaced_ids))
                .values(base_transcript_id=None)
            )
            session.execute(delete(TranscriptRow).where(TranscriptRow.id.in_(replaced_ids)))
        session.add(
            TranscriptRow(
                file_id=file_id,
                provider=transcript.provider,
                model=transcript.model,
                language=transcript.language,
                has_speakers=transcript.has_speakers,
                source="local",
                artifact_kind=artifact_kind,
                text=transcript.text,
                segments=segments,
                profile_snapshot=provenance_run.profile_snapshot if provenance_run else {},
            )
        )
        # Register stable speaker identities; existing display names are kept.
        sync_speakers(session, file_id, speaker_keys_from_segments(segments))
    return transcript


def _persist_summary(file_id: str, result: dict) -> None:
    template = result.get("template", "default")
    with session_scope() as session:
        existing = session.scalar(select(SummaryRow).where(
            SummaryRow.file_id == file_id, SummaryRow.template == template,
            SummaryRow.source == "local",
        ))
        next_revision = 1
        if existing is not None:
            next_revision = (existing.revision or 1) + 1
            session.add(SummaryRevision(
                file_id=file_id, template=existing.template, revision=existing.revision or 1,
                title=existing.title, content_md=existing.content_md,
                llm_provider=existing.llm_provider, model=existing.model, source=existing.source,
                template_version=existing.template_version,
                transcript_revision=existing.transcript_revision,
                profile_snapshot=existing.profile_snapshot or {}, created_at=existing.created_at,
            ))
            session.delete(existing)
        file = session.get(PlaudFile, file_id)
        transcript_revision = (
            file.corrected_transcript.revision if file.corrected_transcript is not None else None
        )
        stage = StageName.mind_map if template == "mind_map" else StageName.summarize
        run = session.scalar(select(StageRun).where(
            StageRun.file_id == file_id, StageRun.stage == stage,
        ))
        session.add(
            SummaryRow(
                file_id=file_id,
                template=template,
                title=result.get("title"),
                content_md=result.get("content_md", ""),
                llm_provider=result.get("provider"),
                model=result.get("model"),
                source="local",
                revision=next_revision,
                template_version=result.get("template_version"),
                transcript_revision=transcript_revision,
                profile_snapshot=run.profile_snapshot if run else {},
            )
        )


def _persist_chunks(file_id: str, transcript: Transcript, settings: Settings) -> str | None:
    chunks = index.build_chunks(transcript)
    if not chunks:
        return None
    blobs, model_name, dim = index.embed_chunks(chunks, settings)
    with session_scope() as session:
        run = session.scalar(select(StageRun).where(
            StageRun.file_id == file_id, StageRun.stage == StageName.index,
        ))
        file = session.get(PlaudFile, file_id)
        selected = _select_raw_transcript(file, settings) if file is not None else None
        revision = file.corrected_transcript_for_source(selected.source) \
            if file is not None and selected is not None else None
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
                    embedding_provider=settings.embeddings.provider,
                    embedding_model=model_name,
                    transcript_revision=revision.revision if revision is not None else 0,
                    dim=dim,
                    embedding=blob,
                    profile_snapshot=run.profile_snapshot if run else {},
                )
            )
    return model_name


def process_pending(
    settings: Settings | None = None, limit: int | None = None, force: bool = False
) -> int:
    """Process newest files in ``downloaded`` state, up to ``pipeline.concurrency``
    at a time. ``limit`` bounds a daemon batch; ``None`` drains the backlog.
    Returns the count that reached either complete or usable-partial state."""
    settings = settings or get_settings()
    with session_scope() as session:
        now = datetime.now(UTC)
        stmt = (
            select(PlaudFile.id)
            .where(or_(
                PlaudFile.status == FileStatus.downloaded,
                (PlaudFile.status == FileStatus.error)
                & (PlaudFile.retry_after.is_not(None))
                & (PlaudFile.retry_after <= now),
            ))
            .order_by(PlaudFile.start_time_ms.desc().nulls_last(), PlaudFile.created_at.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        ids = list(session.scalars(stmt))
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


def generate_note(file_id: str, template: str, settings: Settings | None = None) -> dict:
    """Generate or safely regenerate one note tab without rerunning speech stages."""
    settings = (settings or get_settings()).model_copy(deep=True)
    canonical = _load_transcript(file_id, settings)
    if canonical is None:
        raise ValueError("recording has no canonical transcript")
    transcript, source = canonical
    if source != "local" and settings.pipeline.artifact_mode == "independent":
        raise ValueError("independent mode requires a local transcript")
    settings.pipeline.summary_template = template
    resolution_template = summarize.auto_select_template(transcript) \
        if template == "auto" else template
    _begin_stage(file_id, StageName.summarize, {"template_name": resolution_template})
    try:
        def generate(routed):
            routed.pipeline.summary_template = template
            return summarize.summarize(transcript, routed)

        result, _, route, fallback_index = _run_with_fallback(
            file_id, StageName.summarize, settings, generate
        )
        _persist_summary(file_id, result)
        _finish_stage(file_id, StageName.summarize, provider=result.get("provider"),
                      model=result.get("model"), artifact_source="local",
                      detail={"template": result.get("template"),
                              "coverage": result.get("coverage", {}),
                              "fallback_index": fallback_index,
                              "selected_connection": route.get("connection")})
        return result
    except Exception as exc:
        _fail_stage(file_id, StageName.summarize, exc)
        raise
