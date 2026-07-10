"""Vocabulary/name correction as a provenance-preserving transcript revision."""

from __future__ import annotations

import copy
import json
import re

from sqlalchemy import select

from ..asr.base import Transcript
from ..asr.remote_worker_provider import _parse_transcript
from ..db.models import PlaudFile, TranscriptRevision, VocabularyTerm
from ..db.session import session_scope
from ..llm.base import build_llm
from ..providers.remote_inference import run_remote_json
from .align import _payload


def _replace(text: str, term: VocabularyTerm) -> str:
    if term.case_sensitive:
        return text.replace(term.term, term.replacement)
    return re.sub(re.escape(term.term), lambda _: term.replacement, text,
                  flags=re.IGNORECASE)


def apply_vocabulary(transcript: Transcript, terms: list[VocabularyTerm]) -> Transcript:
    result = copy.deepcopy(transcript)
    ordered = sorted((term for term in terms if term.enabled is not False),
                     key=lambda item: len(item.term), reverse=True)
    for segment in result.segments:
        for term in ordered:
            segment.text = _replace(segment.text, term)
        for word in segment.words:
            for term in ordered:
                word.text = _replace(word.text, term)
    return result


def run(file_id: str, transcript: Transcript) -> tuple[Transcript, dict]:
    with session_scope() as session:
        terms = list(session.scalars(select(VocabularyTerm).where(VocabularyTerm.enabled)))
        if transcript.language:
            terms = [term for term in terms
                     if term.language is None or term.language == transcript.language]
        corrected = apply_vocabulary(transcript, terms)
    return _persist(file_id, transcript, corrected, {"terms": len(terms)},
                    "automatic vocabulary correction")


def run_remote(file_id: str, transcript: Transcript, settings) -> tuple[Transcript, dict]:
    try:
        data = run_remote_json(settings.llm.remote_worker, "correction",
                               {"transcript": _payload(transcript)})
        corrected = _parse_transcript(data, settings.llm.remote_worker.model)
    except Exception as exc:
        raise RuntimeError(f"remote transcript correction failed: {exc}") from exc
    _validate_shape(transcript, corrected)
    corrected.provider = transcript.provider
    corrected.model = transcript.model
    corrected.has_speakers = transcript.has_speakers
    return _persist(file_id, transcript, corrected, {"remote": True},
                    "remote transcript correction")


def run_llm(file_id: str, transcript: Transcript, settings) -> tuple[Transcript, dict]:
    prompt = (
        "Correct names, terminology, punctuation, and obvious recognition errors in this "
        "transcript JSON. Preserve segment/word count, all timestamps, speakers, and every "
        "non-text field exactly. Return only the complete corrected JSON object.\n\n"
        + json.dumps(_payload(transcript), ensure_ascii=False, separators=(",", ":"))
    )
    raw = build_llm(settings.llm).complete(
        prompt, system="Never add facts or alter timing/speaker metadata.",
        temperature=0, max_tokens=8000,
    ).strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        corrected = _parse_transcript(json.loads(raw), transcript.model or "correction")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError("correction provider returned invalid transcript JSON") from exc
    _validate_shape(transcript, corrected)
    corrected.provider = transcript.provider
    corrected.model = transcript.model
    corrected.has_speakers = transcript.has_speakers
    return _persist(file_id, transcript, corrected,
                    {"llm_provider": settings.llm.provider}, "LLM transcript correction")


def _validate_shape(transcript: Transcript, corrected: Transcript) -> None:
    if len(corrected.segments) != len(transcript.segments):
        raise RuntimeError("correction provider changed transcript segmentation")
    for original, changed in zip(transcript.segments, corrected.segments, strict=True):
        if (original.start, original.end, original.speaker) != (
            changed.start, changed.end, changed.speaker
        ):
            raise RuntimeError("correction provider changed timing or speaker identity")
        if len(original.words) != len(changed.words):
            raise RuntimeError("correction provider changed word alignment count")
        for old_word, new_word in zip(original.words, changed.words, strict=True):
            if (old_word.start, old_word.end, old_word.speaker) != (
                new_word.start, new_word.end, new_word.speaker
            ):
                raise RuntimeError("correction provider changed word timing or speaker identity")


def _persist(file_id: str, transcript: Transcript, corrected: Transcript,
             detail: dict, note: str) -> tuple[Transcript, dict]:
    if corrected.text == transcript.text:
        return transcript, {"changed": False, **detail}
    with session_scope() as session:
        file = session.get(PlaudFile, file_id)
        latest = file.corrected_transcript
        if latest is not None and latest.kind == "user":
            # User edits are canonical and must never be silently overwritten.
            return transcript, {"changed": False, **detail,
                                "reason": "user revision preserved"}
        raw = file.local_transcript
        next_revision = max((row.revision for row in file.transcript_revisions), default=0) + 1
        segments = [
            {"text": seg.text, "start": seg.start, "end": seg.end,
             "speaker": seg.speaker,
             "words": [{"text": word.text, "start": word.start, "end": word.end,
                        "speaker": word.speaker, "confidence": word.confidence}
                       for word in seg.words]}
            for seg in corrected.segments
        ]
        session.add(TranscriptRevision(
            file_id=file_id, base_transcript_id=raw.id if raw else None,
            revision=next_revision, source="local", segments=segments,
            text=corrected.text, has_speakers=corrected.has_speakers,
            note=note, kind="vocabulary",
        ))
        return corrected, {"changed": True, **detail, "revision": next_revision}
