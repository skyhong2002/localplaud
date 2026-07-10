"""The worker pipeline — turn a downloaded recording into a knowledge entry.

Stages (each gated by config in ``pipeline``):
    convert -> transcribe -> diarize -> summarize -> index

State lives on the ``PlaudFile`` row; derived artifacts become ``Transcript``,
``Summary`` and ``Chunk`` rows. Stages are resumable: a file is only marked
``done`` when the enabled stages have all produced their artifacts.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select

from ..asr.base import Segment, Transcript, Word
from ..config import Settings, get_settings
from ..db.models import (
    Chunk,
    FileStatus,
    PlaudFile,
    StageName,
    StageRun,
    StageStatus,
)
from ..db.models import Summary as SummaryRow
from ..db.models import Transcript as TranscriptRow
from ..db.session import session_scope
from ..store.files import wav_path
from . import convert, index, summarize, transcribe
from .diarize import DiarizationUnavailable, diarize

log = logging.getLogger(__name__)


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


def _rehydrate_transcript(row: TranscriptRow) -> Transcript:
    segs = []
    for s in row.segments or []:
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
    return Transcript(
        segments=segs,
        language=row.language,
        provider=row.provider,
        model=row.model,
        has_speakers=row.has_speakers,
    )


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
                    transcript = transcribe.run_asr(wav, settings)
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
        elif settings.diarize.provider == "none":
            _skip_stage(file_id, StageName.diarize, "provider disabled")
        else:
            _begin_stage(file_id, StageName.diarize)
            try:
                transcript = diarize(wav, transcript, settings.diarize)
                _persist_transcript(file_id, transcript)
                _finish_stage(
                    file_id,
                    StageName.diarize,
                    provider=settings.diarize.provider,
                    model=settings.diarize.model,
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

        # --- summarize (skip if this template's summary already exists) #
        if pcfg.summarize and transcript is not None:
            if force or not _has_summary(file_id, pcfg.summary_template):
                _begin_stage(file_id, StageName.summarize)
                try:
                    result = summarize.summarize(transcript, settings)
                    _persist_summary(file_id, result)
                    _finish_stage(
                        file_id,
                        StageName.summarize,
                        provider=result.get("provider"),
                        model=result.get("model"),
                        artifact_source="local",
                        detail={"template": result.get("template", "default")},
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
                    detail={"reused": True, "template": pcfg.summary_template},
                )
        else:
            _skip_stage(
                file_id,
                StageName.summarize,
                "disabled" if not pcfg.summarize else "no transcript",
            )

        # --- index (skip if chunks already exist) --------------------- #
        if pcfg.index and transcript is not None:
            if force or not _has_chunks(file_id):
                _begin_stage(file_id, StageName.index)
                try:
                    model_name = _persist_chunks(file_id, transcript, settings)
                    _finish_stage(
                        file_id,
                        StageName.index,
                        provider=settings.embeddings.provider,
                        model=model_name,
                        artifact_source="local",
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


def _load_transcript(file_id: str, settings: Settings) -> tuple[Transcript, str] | None:
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            return None
        local = [item for item in row.transcripts if item.source == "local"]
        if settings.pipeline.artifact_mode == "independent":
            selected = local[-1] if local else None
        elif settings.pipeline.prefer_cloud_artifacts:
            cloud = [item for item in row.transcripts if item.source in {"cloud", "plaud"}]
            selected = cloud[-1] if cloud else (local[-1] if local else None)
        else:
            selected = local[-1] if local else None
        return (_rehydrate_transcript(selected), selected.source) if selected else None


def _has_summary(file_id: str, template: str) -> bool:
    with session_scope() as session:
        return any(
            s.template == template and s.source == "local"
            for s in session.get(PlaudFile, file_id).summaries
        )


def _has_chunks(file_id: str) -> bool:
    from ..db.models import Chunk

    with session_scope() as session:
        return session.query(Chunk.id).filter(Chunk.file_id == file_id).first() is not None


def _persist_transcript(file_id: str, transcript: Transcript) -> None:
    with session_scope() as session:
        # Preserve imported Plaud transcripts for comparison/migration. Only the
        # canonical local ASR result is replaced.
        session.execute(
            delete(TranscriptRow).where(
                TranscriptRow.file_id == file_id, TranscriptRow.source == "local"
            )
        )
        session.add(
            TranscriptRow(
                file_id=file_id,
                provider=transcript.provider,
                model=transcript.model,
                language=transcript.language,
                has_speakers=transcript.has_speakers,
                source="local",
                text=transcript.text,
                segments=[asdict(s) for s in transcript.segments],
            )
        )


def _persist_summary(file_id: str, result: dict) -> None:
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
                title=result.get("title"),
                content_md=result.get("content_md", ""),
                llm_provider=result.get("provider"),
                model=result.get("model"),
                source="local",
            )
        )


def _persist_chunks(file_id: str, transcript: Transcript, settings: Settings) -> str | None:
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
