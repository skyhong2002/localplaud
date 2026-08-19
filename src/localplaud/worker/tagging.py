"""LLM-extracted topic/person/org tags for cross-library filtering.

Runs on the controller's own LLM (the Mac's local ollama) after a summary
exists, extracts a small set of typed tags, and attaches them WITHOUT ever
removing a tag the user added or removed by hand. Gated once per recording via
``PlaudFile.auto_tagged_at`` so a reprocess never re-adds a removed tag.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime

from sqlalchemy import func, select

from ..config import Settings
from ..db.models import PlaudFile, Tag
from ..llm.base import LLMError, build_llm

log = logging.getLogger(__name__)

# Distinct default colours so topic/person/org chips read apart at a glance.
_KIND_COLORS = {"topic": "#3b82f6", "person": "#10b981", "org": "#f59e0b"}
_LIMITS = {"topic": 5, "person": 8, "org": 5}
_TAG_MAXLEN = 80

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "topics": {"type": "array", "items": {"type": "string"}},
        "people": {"type": "array", "items": {"type": "string"}},
        "orgs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["topics", "people", "orgs"],
}

_SYSTEM = (
    "You label meeting notes for a searchable archive. Reply with ONLY a JSON "
    "object and no other text. Use the same language as the notes; for Chinese, "
    "always use Traditional Chinese with Taiwan wording (臺灣正體), never "
    "Simplified."
)


def _prompt(summary: str) -> str:
    return (
        "From these notes, extract labels to filter an archive by:\n"
        "- topics: 2-5 short subject labels (1-4 words each)\n"
        "- people: specific named individuals present or discussed "
        "(real names only, not roles like 'the manager', and NEVER anonymous "
        "diarization labels like SPEAKER_00 / Speaker 1)\n"
        "- orgs: named organizations, companies, teams, or groups\n"
        "Omit anything not clearly present; prefer fewer, high-signal labels. "
        'Output JSON {"topics":[],"people":[],"orgs":[]}.\n\nNotes:\n---\n' + summary + "\n---"
    )


def _parse_json(raw: str) -> dict:
    if not raw:
        return {}
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Anonymous diarization labels the LLM tends to mistake for people.
_JUNK_RE = re.compile(r"^(speaker|說話者|語者|發言者)[\s_]*\d+$", re.IGNORECASE)


def _clean(value: object) -> str | None:
    from ..zh import to_traditional

    if not isinstance(value, str):
        return None
    text = " ".join(value.split())  # collapse whitespace first
    text = text.lstrip("#").strip()  # drop leading markdown heading marks
    text = text.strip("\"'“”「」『』·-").strip()  # strip wrapping quotes/brackets
    if not text or _JUNK_RE.match(text):
        return None
    text = to_traditional(text) or ""
    return text[:_TAG_MAXLEN] or None


def extract_tags(summary: str, settings: Settings) -> dict[str, list[str]]:
    """Return ``{"topic": [...], "person": [...], "org": [...]}``; ``{}`` on any failure."""
    if not summary or not summary.strip():
        return {}
    try:
        llm = build_llm(settings.llm)
        raw = llm.complete(
            _prompt(summary),
            system=_SYSTEM,
            temperature=0.1,
            max_tokens=400,
            json_schema=_SCHEMA,
        )
    except (LLMError, Exception) as exc:  # noqa: BLE001 - tagging must never break the pipeline
        log.warning("tag extraction failed: %s", exc)
        return {}
    data = _parse_json(raw)
    result: dict[str, list[str]] = {}
    for json_key, kind in (("topics", "topic"), ("people", "person"), ("orgs", "org")):
        seen: set[str] = set()
        picked: list[str] = []
        for item in data.get(json_key) or []:
            name = _clean(item)
            if not name:
                continue
            low = name.casefold()
            if low in seen:
                continue
            seen.add(low)
            picked.append(name)
            if len(picked) >= _LIMITS[kind]:
                break
        result[kind] = picked
    return result


def _get_or_create_tag(session, name: str, kind: str) -> Tag:
    """Reuse an existing tag with the same name (case-insensitive), so the same
    topic/person consolidates across the library; otherwise create it."""
    rows = session.scalars(select(Tag).where(func.lower(Tag.name) == name.casefold())).all()
    if rows:
        same_kind = [t for t in rows if t.kind == kind]
        return (same_kind or rows)[0]
    tag = Tag(name=name, kind=kind, color=_KIND_COLORS.get(kind))
    session.add(tag)
    session.flush()
    return tag


def apply_tags(
    session,
    file_id: str,
    tags_by_kind: dict[str, list[str]] | None,
    *,
    force: bool = False,
) -> dict:
    """Attach already-extracted typed tags to a recording — no LLM call.

    Only adds tags (never removes), and runs once per recording unless ``force``,
    so manual tag edits are preserved. This is the controller-side half: the
    extraction (``extract_tags``) runs on whichever host produced the summary
    (the WSL worker), and the result is applied here.
    """
    row = session.get(PlaudFile, file_id)
    if row is None:
        return {"applied": 0, "skipped": "no recording"}
    if row.auto_tagged_at is not None and not force:
        return {"applied": 0, "skipped": "already tagged"}
    row.auto_tagged_at = datetime.now(UTC)
    if not tags_by_kind or not any(tags_by_kind.values()):
        return {"applied": 0, "skipped": "no tags"}
    from ..zh import to_traditional

    existing_ids = {t.id for t in row.tags}
    applied = 0
    for kind in ("topic", "person", "org"):
        for name in tags_by_kind.get(kind) or []:
            # Extraction may run on the worker where OpenCC is absent, so the
            # authoritative Simplified→Traditional conversion happens here.
            name = to_traditional(name) or name
            tag = _get_or_create_tag(session, name, kind)
            if tag.id not in existing_ids:
                row.tags.append(tag)
                existing_ids.add(tag.id)
                applied += 1
    return {"applied": applied, "tags": tags_by_kind}


def apply_auto_tags(
    session, file_id: str, summary: str, settings: Settings, *, force: bool = False
) -> dict:
    """Extract (via LLM) then attach typed tags. Used by the backfill CLI, which
    runs the extraction on the Mac's own LLM."""
    row = session.get(PlaudFile, file_id)
    if row is None:
        return {"applied": 0, "skipped": "no recording"}
    if row.auto_tagged_at is not None and not force:
        return {"applied": 0, "skipped": "already tagged"}
    return apply_tags(session, file_id, extract_tags(summary, settings), force=True)
