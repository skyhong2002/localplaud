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
from pathlib import Path

from sqlalchemy import delete, select

from ..asr.base import Segment, Transcript, Word
from ..config import Settings, get_settings
from ..db.models import Chunk, FileStatus, PlaudFile
from ..db.models import Summary as SummaryRow
from ..db.models import Transcript as TranscriptRow
from ..db.session import session_scope
from ..store.files import wav_path
from . import convert, index, summarize, transcribe
from .diarize import DiarizationUnavailable, diarize

log = logging.getLogger(__name__)


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

    try:
        # --- convert (skip if the wav already exists) ----------------- #
        wav = Path(existing_wav) if existing_wav else wav_path(file_id)
        if pcfg.convert:
            if force or not wav.exists():
                convert.to_wav(audio, wav)
                with session_scope() as session:
                    session.get(PlaudFile, file_id).wav_path = str(wav)
        else:
            wav = audio

        # --- transcribe (reuse an existing transcript to resume) ------ #
        transcript: Transcript | None = None
        if pcfg.transcribe:
            existing = _load_transcript(file_id) if not force else None
            if existing is not None:
                log.info("Reusing existing transcript for %s", file_id)
                transcript = existing
            else:
                transcript = transcribe.run_asr(wav, settings)
                if pcfg.diarize and not transcript.has_speakers:
                    try:
                        transcript = diarize(wav, transcript, settings.diarize)
                    except DiarizationUnavailable as exc:
                        log.warning("Diarization skipped for %s: %s", file_id, exc)
                _persist_transcript(file_id, transcript)

        # --- summarize (skip if this template's summary already exists) #
        if pcfg.summarize and transcript is not None:
            if force or not _has_summary(file_id, pcfg.summary_template):
                result = summarize.summarize(transcript, settings)
                _persist_summary(file_id, result)

        # --- index (skip if chunks already exist) --------------------- #
        if pcfg.index and transcript is not None:
            if force or not _has_chunks(file_id):
                _persist_chunks(file_id, transcript, settings)

        with session_scope() as session:
            session.get(PlaudFile, file_id).status = FileStatus.done
        log.info("Pipeline complete for %s", file_id)

    except Exception as exc:  # noqa: BLE001
        log.exception("Pipeline failed for %s", file_id)
        with session_scope() as session:
            r = session.get(PlaudFile, file_id)
            r.status = FileStatus.error
            r.error = str(exc)[:2000]
        raise


def _load_transcript(file_id: str) -> Transcript | None:
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        return _rehydrate_transcript(row.transcript) if row and row.transcript else None


def _has_summary(file_id: str, template: str) -> bool:
    with session_scope() as session:
        return any(s.template == template for s in session.get(PlaudFile, file_id).summaries)


def _has_chunks(file_id: str) -> bool:
    from ..db.models import Chunk

    with session_scope() as session:
        return session.query(Chunk.id).filter(Chunk.file_id == file_id).first() is not None


def _persist_transcript(file_id: str, transcript: Transcript) -> None:
    with session_scope() as session:
        session.execute(delete(TranscriptRow).where(TranscriptRow.file_id == file_id))
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
                SummaryRow.file_id == file_id, SummaryRow.template == template
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


def _persist_chunks(file_id: str, transcript: Transcript, settings: Settings) -> None:
    chunks = index.build_chunks(transcript)
    if not chunks:
        return
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


def process_pending(
    settings: Settings | None = None, limit: int | None = None, force: bool = False
) -> int:
    """Process all files in ``downloaded`` state, up to ``pipeline.concurrency``
    at a time. Returns count processed successfully."""
    settings = settings or get_settings()
    with session_scope() as session:
        stmt = select(PlaudFile.id).where(PlaudFile.status == FileStatus.downloaded)
        if limit:
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
