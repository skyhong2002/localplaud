"""Provenance-aware lexical search that works without embeddings or an LLM."""

from __future__ import annotations

from sqlalchemy import or_, select

from .config import Settings, get_settings
from .db.models import PlaudFile, StageName, Tag
from .db.session import session_scope
from .worker.pipeline import _select_raw_transcript


def _excerpt(text: str, query: str, radius: int = 90) -> str:
    normalized = text.casefold()
    index = normalized.find(query.casefold())
    if index < 0:
        return text[: radius * 2].strip()
    start, end = max(0, index - radius), min(len(text), index + len(query) + radius)
    prefix, suffix = ("…" if start else ""), ("…" if end < len(text) else "")
    return prefix + text[start:end].strip() + suffix


def lexical_search(
    query: str,
    *,
    folder_id: int | None = None,
    tag_id: int | None = None,
    origin: str | None = None,
    date_from_ms: int | None = None,
    date_to_ms: int | None = None,
    limit: int = 100,
    settings: Settings | None = None,
) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    settings = settings or get_settings()
    needle = query.casefold()
    with session_scope() as session:
        stmt = select(PlaudFile).where(PlaudFile.is_trash.is_(False))
        if folder_id is not None:
            stmt = stmt.where(PlaudFile.folder_id == folder_id)
        if tag_id is not None:
            stmt = stmt.where(PlaudFile.tags.any(Tag.id == tag_id))
        if origin == "plaud":
            stmt = stmt.where(or_(PlaudFile.origin == "plaud", PlaudFile.origin.is_(None)))
        elif origin == "local":
            stmt = stmt.where(PlaudFile.origin == origin)
        if date_from_ms is not None:
            stmt = stmt.where(PlaudFile.start_time_ms >= date_from_ms)
        if date_to_ms is not None:
            stmt = stmt.where(PlaudFile.start_time_ms < date_to_ms)
        rows = list(session.scalars(stmt.order_by(PlaudFile.start_time_ms.desc().nullslast())))
        hits: list[dict] = []
        for row in rows:
            common = {"file_id": row.id, "filename": row.display_title}
            if needle in row.display_title.casefold() or needle in (row.filename or "").casefold():
                hits.append(
                    common
                    | {
                        "kind": "title",
                        "score": 1.0,
                        "text": row.display_title,
                        "start": None,
                        "end": None,
                        "speaker": None,
                    }
                )
            raw = _select_raw_transcript(row, settings)
            revision = row.corrected_transcript_for_source(raw.source) if raw else None
            segments = revision.segments if revision else (raw.segments if raw else [])
            transcript_hits = 0
            for segment in segments or []:
                text = str(segment.get("text") or "")
                if needle not in text.casefold():
                    continue
                hits.append(
                    common
                    | {
                        "kind": "transcript",
                        "score": 0.9,
                        "text": _excerpt(text, query),
                        "start": segment.get("start"),
                        "end": segment.get("end"),
                        "speaker": segment.get("speaker"),
                    }
                )
                transcript_hits += 1
                if transcript_hits >= 3:
                    break
            stale = {
                run.stage for run in row.stage_runs if (run.detail or {}).get("stale")
            }
            summaries = (
                [item for item in row.summaries if item.source == "local"]
                if settings.pipeline.artifact_mode == "independent"
                else row.summaries
            )
            note_hits = 0
            for note in summaries:
                if note.source == "local" and (
                    (note.template == "mind_map" and StageName.mind_map in stale)
                    or (note.template != "mind_map" and StageName.summarize in stale)
                ):
                    continue
                if needle not in note.content_md.casefold():
                    continue
                hits.append(
                    common
                    | {
                        "kind": "note",
                        "score": 0.75,
                        "text": _excerpt(note.content_md, query),
                        "start": None,
                        "end": None,
                        "speaker": None,
                    }
                )
                note_hits += 1
                if note_hits >= 2:
                    break
            for note in row.user_notes:
                text = f"{note.title}\n{note.content_md}"
                if needle not in text.casefold():
                    continue
                hits.append(
                    common
                    | {
                        # User-owned saved notes are a distinct result kind from
                        # generated notes so results can label them honestly.
                        "kind": "saved_note",
                        "score": 0.8,
                        "text": _excerpt(text, query),
                        "start": None,
                        "end": None,
                        "speaker": None,
                    }
                )
                note_hits += 1
                if note_hits >= 2:
                    break
            if len(hits) >= limit:
                break
    return sorted(hits[:limit], key=lambda hit: (-hit["score"], hit["filename"]))
