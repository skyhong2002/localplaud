"""The worker pipeline — turn a downloaded recording into a knowledge entry.

Stages (each gated by config in ``pipeline``):
    convert -> transcribe -> diarize -> summarize -> mind_map -> index

State lives on the ``PlaudFile`` row; derived artifacts become ``Transcript``,
``Summary`` and ``Chunk`` rows. Stages are resumable: a file is only marked
``done`` when the enabled stages have all produced their artifacts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select, update

from ..asr.base import Segment, Transcript, Word
from ..config import Settings, get_settings
from ..db.models import (
    Chunk,
    FileStatus,
    PlaudFile,
    ProviderConnection,
    StageName,
    StageRun,
    StageStatus,
    TranscriptRevision,
)
from ..db.models import Summary as SummaryRow
from ..db.models import Transcript as TranscriptRow
from ..db.session import session_scope
from ..providers.service import resolve_recording_profile
from ..remote.client import RemoteWorkerClient
from ..remote.protocol import InputReference, JobStage, JobSubmitRequest
from ..store.files import wav_path
from ..store.speakers import speaker_keys_from_segments, sync_speakers
from . import convert, index, mindmap, summarize, summary_templates, transcribe
from .diarize import DiarizationUnavailable, diarize

log = logging.getLogger(__name__)
_PROFILE_SNAPSHOT: ContextVar[dict | None] = ContextVar("resolved_profile_snapshot", default=None)


def _settings_for_stage(
    settings: Settings, snapshot: dict, stage: str
) -> Settings:
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
    provider = str(selected["connection"]).split(":", 1)[-1]
    family_config.provider = provider
    provider_config = getattr(family_config, provider.replace("-", "_"), None)
    if provider_config is not None and hasattr(provider_config, "model"):
        provider_config.model = selected.get("model")
    elif hasattr(family_config, "model"):
        family_config.model = selected.get("model")

    for key, value in selected.get("options", {}).items():
        target = provider_config if provider_config is not None and hasattr(provider_config, key) else family_config
        if hasattr(target, key):
            setattr(target, key, value)

    if stage == "transcribe":
        policy = snapshot.get("policy", {})
        fallback = policy.get("fallback_policy", {}).get("asr", [])
        family_config.fallback = [] if policy.get("no_egress") else list(fallback)
    return resolved


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
            {"file": file_id, "stage": stage, "selection": selection, "inputs": [i.sha256 for i in inputs]},
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
        if begin_attempt:
            run.attempts = (run.attempts or 0) + 1
            run.started_at = now
            run.completed_at = None
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
        provider=base.provider if base is not None else "local-edit",
        model=base.model if base is not None else None,
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


def process_file(file_id: str, settings: Settings | None = None, force: bool = False) -> None:
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

    profile_token = _PROFILE_SNAPSHOT.set(snapshot)
    transcribe_settings = _settings_for_stage(settings, snapshot, "transcribe")
    diarize_settings = _settings_for_stage(settings, snapshot, "diarize")
    summarize_settings = _settings_for_stage(settings, snapshot, "summarize")
    mind_map_settings = _settings_for_stage(settings, snapshot, "mind_map")
    summarize_settings.pipeline.summary_template = template_key
    mind_map_settings.pipeline.summary_template = template_key
    embed_settings = _settings_for_stage(settings, snapshot, "embed")

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
                    if _remote_selection(snapshot, "transcribe"):
                        payload = _run_remote_stage(
                            file_id, snapshot, "transcribe", [_remote_audio_input(wav)]
                        )
                        transcript = Transcript(
                            segments=_rehydrate_segments(payload.get("segments")),
                            language=payload.get("language"),
                            duration=payload.get("duration"),
                            provider="remote-worker",
                            model=payload.get("model"),
                            has_speakers=payload.get("has_speakers", False),
                        )
                    else:
                        transcript = transcribe.run_asr(wav, transcribe_settings)
                    _persist_transcript(file_id, transcript)
                    _finish_stage(
                        file_id,
                        StageName.transcribe,
                        provider=transcript.provider,
                        model=transcript.model,
                        artifact_source="local",
                    )
                except Exception as exc:
                    _fail_stage(file_id, StageName.transcribe, exc)
                    raise
        else:
            _skip_stage(file_id, StageName.transcribe, "disabled")

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
        elif diarize_settings.diarize.provider == "none":
            _skip_stage(file_id, StageName.diarize, "provider disabled")
        else:
            _begin_stage(file_id, StageName.diarize)
            try:
                if _remote_selection(snapshot, "diarize"):
                    payload = _run_remote_stage(
                        file_id,
                        snapshot,
                        "diarize",
                        [
                            _remote_audio_input(wav),
                            _remote_json_input("transcript", _transcript_payload(transcript)),
                        ],
                    )
                    transcript.segments = _rehydrate_segments(payload.get("segments"))
                    transcript.has_speakers = payload.get("has_speakers", True)
                    transcript.provider = "remote-worker"
                    transcript.model = snapshot["stages"]["diarize"].get("model")
                else:
                    transcript = diarize(wav, transcript, diarize_settings.diarize)
                _persist_transcript(file_id, transcript)
                _finish_stage(
                    file_id,
                    StageName.diarize,
                    provider=(
                        "remote-worker"
                        if _remote_selection(snapshot, "diarize")
                        else diarize_settings.diarize.provider
                    ),
                    model=(
                        snapshot["stages"]["diarize"].get("model")
                        if _remote_selection(snapshot, "diarize")
                        else diarize_settings.diarize.model
                    ),
                    artifact_source="local",
                )
            except DiarizationUnavailable as exc:
                log.warning("Diarization degraded for %s: %s", file_id, exc)
                _fail_stage(file_id, StageName.diarize, exc, degraded=True)
                partial_errors.append(f"diarize: {exc}")
            except Exception as exc:  # noqa: BLE001 - preserve usable transcript
                log.exception("Diarization failed for %s", file_id)
                _fail_stage(file_id, StageName.diarize, exc)
                partial_errors.append(f"diarize: {exc}")

        # A force rebuild may replace the raw row while preserving user edits.
        # Reload the configured canonical lane before all derived stages so notes,
        # mind maps and the search index never drift from corrected transcript UI.
        canonical = _load_transcript(file_id, settings)
        transcript_lineage = _transcript_lineage(file_id, settings)
        if canonical is not None:
            transcript, transcript_source = canonical
        auto_recommendation = None
        if requested_template_key == "auto":
            from ..template_auto import recommend_template

            auto_recommendation = recommend_template(
                title=row.filename or "",
                transcript=transcript.text if transcript is not None else "",
                duration_ms=row.duration_ms,
            )
            template_key = auto_recommendation["key"]
            summarize_settings.pipeline.summary_template = template_key
            mind_map_settings.pipeline.summary_template = template_key

        # --- summarize (skip if this template's summary already exists) #
        if pcfg.summarize and transcript is not None:
            if force or not _has_summary(file_id, template_key):
                _begin_stage(file_id, StageName.summarize)
                try:
                    if _remote_selection(snapshot, "summarize"):
                        result = _run_remote_stage(
                            file_id,
                            snapshot,
                            "summarize",
                            [_remote_json_input("transcript", _transcript_payload(transcript))],
                            options={
                                "template": summary_templates.template_snapshot(
                                    summary_templates.get_effective_template(template_key)
                                )
                            },
                        )
                        result.setdefault("provider", "remote-worker")
                        result.setdefault("model", snapshot["stages"]["summarize"].get("model"))
                    else:
                        result = summarize.summarize(transcript, summarize_settings)
                    _persist_summary(file_id, result, transcript_lineage)
                    _finish_stage(
                        file_id,
                        StageName.summarize,
                        provider=result.get("provider"),
                        model=result.get("model"),
                        artifact_source="local",
                        detail={
                            "template": result.get("template", "default"),
                            "coverage": result.get("coverage", {}),
                            "transcript": transcript_lineage,
                            "auto_template": auto_recommendation,
                        },
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
                _begin_stage(file_id, StageName.mind_map)
                try:
                    summary_md = _load_summary_md(file_id, template_key)
                    if _remote_selection(snapshot, "mind_map"):
                        result = _run_remote_stage(
                            file_id,
                            snapshot,
                            "mind_map",
                            [_remote_json_input("transcript", _transcript_payload(transcript))],
                            options={"summary_md": summary_md},
                        )
                        result.setdefault("provider", "remote-worker")
                        result.setdefault("model", snapshot["stages"]["mind_map"].get("model"))
                    else:
                        result = mindmap.generate_mind_map(
                            transcript, mind_map_settings, summary_md
                        )
                    _persist_summary(file_id, result, transcript_lineage)
                    _finish_stage(
                        file_id,
                        StageName.mind_map,
                        provider=result.get("provider"),
                        model=result.get("model"),
                        artifact_source="local",
                        detail=result.get("detail", {})
                        | {
                            "transcript": transcript_lineage,
                            "auto_template": auto_recommendation,
                        },
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
                    if _remote_selection(snapshot, "embed"):
                        payload = _run_remote_stage(
                            file_id,
                            snapshot,
                            "embed",
                            [_remote_json_input("transcript", _transcript_payload(transcript))],
                        )
                        model_name = _persist_remote_chunks(file_id, payload, transcript_lineage)
                    else:
                        model_name = _persist_chunks(
                            file_id, transcript, embed_settings, transcript_lineage
                        )
                    _finish_stage(
                        file_id,
                        StageName.index,
                        provider=(
                            "remote-worker"
                            if _remote_selection(snapshot, "embed")
                            else embed_settings.embeddings.provider
                        ),
                        model=model_name,
                        artifact_source="local",
                        detail={"transcript": transcript_lineage},
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
            row = session.get(PlaudFile, file_id)
            row.status = FileStatus.partial if partial_errors else FileStatus.done
            row.error = "; ".join(partial_errors)[:2000] if partial_errors else None
        log.info("Pipeline %s for %s", "partial" if partial_errors else "complete", file_id)

    except Exception as exc:  # noqa: BLE001
        log.exception("Pipeline failed for %s", file_id)
        with session_scope() as session:
            r = session.get(PlaudFile, file_id)
            r.status = FileStatus.error
            r.error = str(exc)[:2000]
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
            speaker.key: speaker.display_name
            for speaker in row.speakers
            if speaker.display_name
        }
        return (_apply_speaker_display_names(transcript, names), selected.source)


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
            and (
                template == "mind_map"
                or (s.template_version or 1) == expected_version
            )
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


def _persist_transcript(file_id: str, transcript: Transcript) -> None:
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
        if replaced_ids:
            session.execute(
                update(TranscriptRevision)
                .where(TranscriptRevision.base_transcript_id.in_(replaced_ids))
                .values(base_transcript_id=None)
            )
            session.execute(delete(TranscriptRow).where(TranscriptRow.id.in_(replaced_ids)))
        segments = [asdict(s) for s in transcript.segments]
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


def _persist_remote_chunks(
    file_id: str, payload: dict, lineage: dict | None = None
) -> str | None:
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
    """Process newest files in ``downloaded`` state, up to ``pipeline.concurrency``
    at a time. ``limit`` bounds a daemon batch; ``None`` drains the backlog.
    Returns the count that reached either complete or usable-partial state."""
    settings = settings or get_settings()
    with session_scope() as session:
        stmt = (
            select(PlaudFile.id)
            .where(PlaudFile.status == FileStatus.downloaded)
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
