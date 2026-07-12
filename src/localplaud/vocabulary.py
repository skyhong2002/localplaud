"""Non-destructive custom-vocabulary correction for canonical transcripts."""

from __future__ import annotations

import copy
import re

from sqlalchemy import delete, select

from .config import Settings, get_settings
from .db.models import (
    Chunk,
    PlaudFile,
    StageName,
    StageRun,
    StageStatus,
    Transcript,
    TranscriptRevision,
    VocabularyTerm,
)
from .db.session import session_scope
from .worker.pipeline import _select_raw_transcript


def _language_matches(rule_language: str | None, transcript_language: str | None) -> bool:
    if not rule_language:
        return True
    if not transcript_language:
        return False
    rule = rule_language.casefold().replace("_", "-")
    actual = transcript_language.casefold().replace("_", "-")
    return actual == rule or actual.startswith(f"{rule}-") or rule.startswith(f"{actual}-")


def _correct_text(text: str, terms: list[VocabularyTerm]) -> tuple[str, int, set[int]]:
    """Apply non-overlapping matches from the original text, longest match first."""
    candidates: list[tuple[int, int, int, VocabularyTerm]] = []
    for order, term in enumerate(terms):
        flags = 0 if term.case_sensitive else re.IGNORECASE
        for match in re.finditer(re.escape(term.source_text), text, flags=flags):
            candidates.append((match.start(), match.end(), order, term))
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    selected: list[tuple[int, int, VocabularyTerm]] = []
    cursor = 0
    for start, end, _order, term in candidates:
        if start < cursor:
            continue
        selected.append((start, end, term))
        cursor = end
    if not selected:
        return text, 0, set()
    parts: list[str] = []
    cursor = 0
    used: set[int] = set()
    for start, end, term in selected:
        parts.extend((text[cursor:start], term.replacement_text))
        cursor = end
        used.add(term.id)
    parts.append(text[cursor:])
    return "".join(parts), len(selected), used


def correct_segments(
    segments: list[dict], terms: list[VocabularyTerm], language: str | None
) -> tuple[list[dict], int, list[int]]:
    """Return corrected segments, replacement count, and applied rule ids."""
    corrected = copy.deepcopy(segments or [])
    count = 0
    used: set[int] = set()
    applicable = sorted(
        (
            term
            for term in terms
            if term.enabled is not False and _language_matches(term.language, language)
        ),
        key=lambda term: len(term.source_text),
        reverse=True,
    )
    for index, segment in enumerate(corrected):
        original = str(segment.get("text") or "")
        text, replacements, segment_rules = _correct_text(original, applicable)
        if replacements:
            count += replacements
            used.update(segment_rules)
            corrected[index] = dict(segment) | {"text": text, "words": []}
    return corrected, count, sorted(used)


def _mark_derived_stale(session, file_id: str, *, reason: str = "vocabulary") -> None:
    session.execute(delete(Chunk).where(Chunk.file_id == file_id))
    for stage in (StageName.summarize, StageName.mind_map, StageName.index):
        run = session.scalar(
            select(StageRun).where(StageRun.file_id == file_id, StageRun.stage == stage)
        )
        if run is None:
            run = StageRun(file_id=file_id, stage=stage, attempts=0, detail={})
            session.add(run)
        run.status = StageStatus.pending
        run.error = None
        run.completed_at = None
        run.detail = dict(run.detail or {}) | {"stale": True, "reason": reason}


def apply_vocabulary(
    file_id: str,
    *,
    automatic: bool = False,
    settings: Settings | None = None,
) -> dict:
    """Apply current rules as one immutable revision.

    Automatic application never supersedes an existing correction. Explicit
    application starts from the latest canonical revision and can therefore update
    existing recordings without destroying earlier edits.
    """
    settings = settings or get_settings()
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise ValueError("recording not found")
        raw = _select_raw_transcript(row, settings)
        if raw is None or raw.source != "local":
            return {"file_id": file_id, "replacements": 0, "revision": None, "rules": []}
        current = row.corrected_transcript_for_source(raw.source)
        if automatic and current is not None:
            return {
                "file_id": file_id,
                "replacements": 0,
                "revision": current.revision,
                "rules": [],
                "skipped": "existing correction",
            }
        base_segments = current.segments if current is not None else raw.segments
        terms = list(
            session.scalars(
                select(VocabularyTerm)
                .where(VocabularyTerm.enabled.is_(True))
                .order_by(VocabularyTerm.id)
            )
        )
        segments, replacements, rule_ids = correct_segments(base_segments, terms, raw.language)
        if not replacements:
            return {"file_id": file_id, "replacements": 0, "revision": None, "rules": []}
        next_revision = max((item.revision for item in row.transcript_revisions), default=0) + 1
        text = "\n".join(
            str(segment.get("text") or "").strip()
            for segment in segments
            if str(segment.get("text") or "").strip()
        )
        mode = "auto" if automatic else "manual"
        session.add(
            TranscriptRevision(
                file_id=file_id,
                base_transcript_id=raw.id,
                revision=next_revision,
                source=raw.source,
                segments=segments,
                text=text,
                has_speakers=current.has_speakers if current is not None else raw.has_speakers,
                note=f"vocabulary:{mode} rules={','.join(map(str, rule_ids))}",
                kind="vocabulary",
                provider="local-vocabulary",
                prompt_version="vocabulary/v1",
            )
        )
        _mark_derived_stale(session, file_id)
        return {
            "file_id": file_id,
            "replacements": replacements,
            "revision": next_revision,
            "rules": rule_ids,
        }


def apply_vocabulary_to_library(settings: Settings | None = None) -> dict:
    """Explicitly apply current rules to every local canonical transcript."""
    settings = settings or get_settings()
    with session_scope() as session:
        file_ids = list(
            session.scalars(
                select(Transcript.file_id)
                .join(PlaudFile, PlaudFile.id == Transcript.file_id)
                .where(PlaudFile.is_trash.is_(False), Transcript.source == "local")
                .distinct()
                .order_by(PlaudFile.start_time_ms.desc().nullslast())
            )
        )
    results = [apply_vocabulary(file_id, settings=settings) for file_id in file_ids]
    changed = [item for item in results if item["replacements"]]
    return {
        "scanned": len(results),
        "changed": len(changed),
        "replacements": sum(item["replacements"] for item in changed),
        "files": changed,
    }
