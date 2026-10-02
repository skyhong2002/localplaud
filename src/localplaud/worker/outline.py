"""Outline stage: timestamped chapter titles for a whole recording.

The outline is an ordered, contiguous list of ``{"start_ms", "end_ms",
"title"}`` chapters that together cover the complete canonical transcript, from
0 to the end of the last segment (or the recording duration, if longer).

Two clearly distinct methods exist:

``llm``
    The configured text LLM reads the transcript in bounded, timestamped parts
    (every segment is sent exactly once, so long recordings are never
    truncated) and marks where topics change. When a long recording yields too
    many chapters, a second LLM pass consolidates the ordered chapter list.

``time_slices``
    An explicitly selected deterministic alternative with no model involved:
    fixed time windows snapped to segment boundaries, each titled by its first
    salient sentence. It is labelled as such in provenance and must never be
    used as a silent stand-in for a failed LLM outline.

Chapter boundaries are always segment starts, so coverage is structural: each
chapter ends where the next begins and the final chapter ends at the end of the
recording. The tail cannot be dropped.
"""

from __future__ import annotations

import json
import logging
import math
import re

from ..asr.base import Segment
from ..asr.base import Transcript as AsrTranscript
from ..config import Settings
from ..llm.base import LLMOutputInvalid, build_llm
from .summarize import _llm_provider_model, _summary_chunk_chars

log = logging.getLogger(__name__)

PROMPT_VERSION = "outline/v1"
TIME_SLICES_VERSION = "outline-time-slices/v1"
TIME_SLICES_PROVIDER = "localplaud"
TIME_SLICES_MODEL = "time-slices"

MAX_CHAPTERS = 40
# Starts closer than this to the previous chapter are folded into it.
MIN_CHAPTER_GAP_MS = 30_000
_TITLE_MAX = 120

_SYSTEM = (
    "You divide a recording transcript into chapters with short, specific topic "
    "titles. Reply with ONLY a JSON object and no other text. Never invent "
    "topics that are not in the transcript. Write titles in the transcript's "
    "dominant language; for Chinese always use Traditional Chinese with Taiwan "
    "wording (臺灣正體), never Simplified."
)

_MAP_PROMPT = """\
Split transcript part {part} of {total} into chapters.

Each line starts with [segment id @ H:MM:SS]. This part runs from {start} to
{end} (about {minutes} minutes). Mark where the topic changes and give every
chapter a short, specific title (at most about 12 words, or 20 Chinese
characters) naming what is discussed.

Rules:
- Aim for about {target} chapter(s) in this part and never fewer than 1.
- "start_segment" must be a segment id from this part; ids strictly increase.
{first_rule}- Output exactly: {{"chapters": [{{"start_segment": <id>, "title": "<title>"}}]}}

Transcript part:
---
{text}
---
"""

_FIRST_RULE = "- The first chapter must start at segment {first_id}.\n"

_CONSOLIDATE_PROMPT = """\
These are the ordered chapters of one complete recording, as
"index. [H:MM:SS] title". There are too many. Merge adjacent chapters about the
same topic into at most {limit} chapters that still cover the whole recording.

Rules:
- Each output chapter starts at one of the listed indexes; indexes strictly
  increase and the first must be 0.
- Give each merged chapter a short, specific title in the chapters' language.
- Output exactly: {{"chapters": [{{"start_index": <index>, "title": "<title>"}}]}}

Chapters:
---
{text}
---
"""

_MAP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "start_segment": {"type": "integer"},
                    "title": {"type": "string"},
                },
                "required": ["start_segment", "title"],
            },
        }
    },
    "required": ["chapters"],
}

_CONSOLIDATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "start_index": {"type": "integer"},
                    "title": {"type": "string"},
                },
                "required": ["start_index", "title"],
            },
        }
    },
    "required": ["chapters"],
}


def format_clock(ms: int) -> str:
    seconds = max(0, int(ms // 1000))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _segments(transcript: AsrTranscript) -> list[Segment]:
    segments = [segment for segment in transcript.segments if (segment.text or "").strip()]
    if not segments:
        raise ValueError("transcript has no text segments to outline")
    previous = -1.0
    for segment in segments:
        start, end = float(segment.start), float(segment.end)
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
            or start < previous
        ):
            raise ValueError("outline requires ordered, finite, positive transcript timestamps")
        previous = start
    return segments


def _end_ms(segments: list[Segment], duration_ms: int | None) -> int:
    last = max(int(round(float(segment.end or segment.start or 0) * 1000)) for segment in segments)
    if duration_ms is not None and duration_ms > 0 and last > duration_ms:
        raise ValueError("transcript timestamps exceed the playable recording duration")
    return max(last, int(duration_ms or 0))


def _start_ms(segment: Segment) -> int:
    return max(0, int(round(float(segment.start or 0) * 1000)))


def _target_total(end_ms: int) -> int:
    """Roughly one chapter per five minutes, bounded to a readable list."""
    return max(1, min(MAX_CHAPTERS, round(end_ms / 300_000) or 1))


def _clean_title(value: object) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip().strip("\"'「」“”")
    title = re.sub(r"^(?:#+|[-*•]|\d+[.)、])\s*", "", title).strip()
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1].rstrip() + "…"
    return title


def _normalize_script(title: str, language: str | None) -> str:
    if not title or not ((language or "").lower().startswith("zh") or re.search(r"[一-鿿]", title)):
        return title
    from ..zh import to_traditional

    return to_traditional(title) or title


def _parse_json(raw: str) -> object:
    text = (raw or "").strip()
    fenced = re.match(r"^```[^\n]*\n(.*?)\n?```\s*$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except (json.JSONDecodeError, ValueError):
                continue
    raise LLMOutputInvalid("outline response was not valid JSON")


def _items(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        payload = payload.get("chapters")
    if not isinstance(payload, list):
        raise LLMOutputInvalid("outline response has no chapter list")
    return [item for item in payload if isinstance(item, dict)]


def _parse_map(raw: str, valid_ids: range) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for item in _items(_parse_json(raw)):
        seg_id = item.get("start_segment")
        if type(seg_id) is not int or not isinstance(item.get("title"), str):
            continue
        title = _clean_title(item.get("title"))
        if seg_id not in valid_ids or not title:
            continue
        if result and seg_id <= result[-1][0]:
            continue
        result.append((seg_id, title))
    if not result:
        raise LLMOutputInvalid("outline part returned no usable chapters")
    return result


def _parse_consolidation(raw: str, count: int) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for item in _items(_parse_json(raw)):
        index = item.get("start_index")
        if type(index) is not int or not isinstance(item.get("title"), str):
            continue
        title = _clean_title(item.get("title"))
        if not 0 <= index < count or not title:
            continue
        if result and index <= result[-1][0]:
            continue
        result.append((index, title))
    if not result:
        raise LLMOutputInvalid("outline consolidation returned no usable chapters")
    if result[0][0] != 0:
        result[0] = (0, result[0][1])
    return result


def _parts(segments: list[Segment], budget: int) -> list[tuple[int, int, str]]:
    """Split segment lines into ordered parts of at most ``budget`` characters.

    Returns ``(first_id, last_id_exclusive, text)``. Every segment appears in
    exactly one part. A single segment longer than the budget forms its own
    part and is sent whole rather than clipped.
    """
    parts: list[tuple[int, int, str]] = []
    lines: list[str] = []
    size = 0
    first = 0
    for idx, segment in enumerate(segments):
        who = f"{segment.speaker}: " if segment.speaker else ""
        line = f"[{idx} @ {format_clock(_start_ms(segment))}] {who}{segment.text.strip()}"
        if lines and size + len(line) + 1 > budget:
            parts.append((first, idx, "\n".join(lines)))
            lines, size, first = [], 0, idx
        lines.append(line)
        size += len(line) + 1
    if lines:
        parts.append((first, len(segments), "\n".join(lines)))
    return parts


def _fold_close_starts(
    starts: list[tuple[int, str]], segments: list[Segment]
) -> list[tuple[int, str]]:
    folded: list[tuple[int, str]] = []
    for seg_id, title in starts:
        if (
            folded
            and _start_ms(segments[seg_id]) - _start_ms(segments[folded[-1][0]])
            < MIN_CHAPTER_GAP_MS
        ):
            continue
        folded.append((seg_id, title))
    return folded


def _merge_to_limit(
    starts: list[tuple[int, str]], segments: list[Segment], end_ms: int, limit: int
):
    """Deterministically merge the shortest chapters into their predecessor.

    Only used when the LLM consolidation pass cannot produce a valid list; the
    surviving titles are still the model's own titles.
    """
    merged = list(starts)
    while len(merged) > limit:
        lengths = []
        for position, (seg_id, _title) in enumerate(merged):
            nxt = (
                _start_ms(segments[merged[position + 1][0]])
                if position + 1 < len(merged)
                else end_ms
            )
            lengths.append(nxt - _start_ms(segments[seg_id]))
        shortest = min(range(1, len(merged)), key=lambda position: lengths[position])
        del merged[shortest]
    return merged


def _chapters(
    starts: list[tuple[int, str]], segments: list[Segment], end_ms: int, language: str | None
) -> list[dict]:
    chapters = []
    for position, (seg_id, title) in enumerate(starts):
        start_ms = 0 if position == 0 else _start_ms(segments[seg_id])
        next_ms = (
            _start_ms(segments[starts[position + 1][0]]) if position + 1 < len(starts) else end_ms
        )
        chapters.append(
            {
                "start_ms": start_ms,
                "end_ms": max(start_ms, next_ms),
                "title": _normalize_script(title, language),
            }
        )
    return chapters


def validate_chapters(chapters: list[dict], end_ms: int) -> None:
    """Assert the structural full-coverage contract."""
    if not chapters:
        raise ValueError("outline has no chapters")
    if chapters[0]["start_ms"] != 0:
        raise ValueError("outline does not start at 0")
    for chapter in chapters:
        if (
            not isinstance(chapter["start_ms"], int)
            or not isinstance(chapter["end_ms"], int)
            or not 0 <= chapter["start_ms"] < chapter["end_ms"] <= end_ms
            or not chapter["title"].strip()
        ):
            raise ValueError("outline has invalid chapter bounds or an empty title")
    for previous, current in zip(chapters, chapters[1:], strict=False):
        if previous["end_ms"] != current["start_ms"]:
            raise ValueError("outline chapters are not contiguous")
    if chapters[-1]["end_ms"] < end_ms:
        raise ValueError("outline does not cover the end of the recording")


def projected_usage(transcript: AsrTranscript, settings: Settings, *, remote: bool = False) -> dict:
    """Reserve for both validation attempts and the optional consolidation pass."""
    segments = _segments(transcript)
    # Unknown remote context size: reserve for one part per segment, an upper
    # bound that avoids constructing a local substitute for the remote model.
    budget = 1 if remote else _summary_chunk_chars(settings, build_llm(settings.llm))
    parts = _parts(segments, budget)
    calls = 2 * (len(parts) + 1)
    return {
        "input_chars": 2 * sum(len(part[2]) + 2000 for part in parts)
        + 2 * len(segments) * 180
        + 4000,
        "output_tokens": 4000 * calls,
        "requests": calls,
        "projection": True,
    }


def generate_outline(
    transcript: AsrTranscript,
    settings: Settings,
    *,
    duration_ms: int | None = None,
) -> dict:
    """Return ``{chapters, method, provider, model, prompt_version, language, detail}``."""
    segments = _segments(transcript)
    end_ms = _end_ms(segments, duration_ms)
    llm = build_llm(settings.llm)
    budget = _summary_chunk_chars(settings, llm)
    parts = _parts(segments, budget)
    total_target = _target_total(end_ms)
    map_calls = 0
    input_chars = output_chars = 0
    starts: list[tuple[int, str]] = []
    for number, (first, stop, text) in enumerate(parts, start=1):
        part_start = _start_ms(segments[first])
        part_end = _start_ms(segments[stop]) if stop < len(segments) else end_ms
        minutes = max(1, round((part_end - part_start) / 60_000))
        target = max(1, round(total_target * (part_end - part_start) / max(end_ms, 1)))
        prompt = _MAP_PROMPT.format(
            part=number,
            total=len(parts),
            start=format_clock(part_start),
            end=format_clock(part_end),
            minutes=minutes,
            target=target,
            first_rule=_FIRST_RULE.format(first_id=first) if number == 1 else "",
            text=text,
        )
        found = None
        last_error: Exception | None = None
        for _attempt in range(2):
            raw = llm.complete(
                prompt,
                system=_SYSTEM,
                temperature=0.1,
                max_tokens=min(4000, 200 + 80 * target * 2),
                json_schema=_MAP_SCHEMA,
            )
            input_chars += len(prompt) + len(_SYSTEM)
            output_chars += len(raw)
            map_calls += 1
            try:
                found = _parse_map(raw, range(first, stop))
                break
            except LLMOutputInvalid as exc:
                last_error = exc
        if found is None:
            raise LLMOutputInvalid(
                f"outline part {number} of {len(parts)} returned no usable chapters"
            ) from last_error
        for seg_id, title in found:
            if starts and seg_id <= starts[-1][0]:
                continue
            starts.append((seg_id, title))

    starts[0] = (0, starts[0][1])
    starts = _fold_close_starts(starts, segments)

    reduce_calls = 0
    consolidation = None
    limit = min(MAX_CHAPTERS, max(total_target + total_target // 2, 6))
    if len(starts) > limit:
        listing = "\n".join(
            f"{index}. [{format_clock(_start_ms(segments[seg_id]))}] {title}"
            for index, (seg_id, title) in enumerate(starts)
        )
        consolidated = None
        consolidation_prompt = _CONSOLIDATE_PROMPT.format(limit=limit, text=listing)
        for _attempt in range(2):
            raw = llm.complete(
                consolidation_prompt,
                system=_SYSTEM,
                temperature=0.1,
                max_tokens=min(4000, 200 + 80 * limit),
                json_schema=_CONSOLIDATE_SCHEMA,
            )
            input_chars += len(consolidation_prompt) + len(_SYSTEM)
            output_chars += len(raw)
            reduce_calls += 1
            try:
                picked = _parse_consolidation(raw, len(starts))
            except LLMOutputInvalid:
                continue
            if len(picked) <= limit:
                consolidated = [(starts[index][0], title) for index, title in picked]
                consolidation = "llm"
                break
        if consolidated is None:
            consolidated = _merge_to_limit(starts, segments, end_ms, limit)
            consolidation = "deterministic-merge"
        starts = consolidated

    chapters = _chapters(starts, segments, end_ms, transcript.language)
    validate_chapters(chapters, end_ms)
    provider, model = _llm_provider_model(settings)
    return {
        "chapters": chapters,
        "method": "llm",
        "source": "local",
        "provider": provider,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "language": transcript.language,
        "detail": {
            "strategy": "direct" if len(parts) == 1 else "hierarchical",
            "parts": len(parts),
            "segments": len(segments),
            "transcript_chars": sum(len(segment.text) for segment in segments),
            "usage": {
                "input_chars": input_chars,
                "output_chars": output_chars,
                "requests": map_calls + reduce_calls,
            },
            "map_calls": map_calls,
            "reduce_calls": reduce_calls,
            "consolidation": consolidation,
            "chapter_count": len(chapters),
            "covered_until_ms": chapters[-1]["end_ms"],
            "last_segment_start_ms": _start_ms(segments[-1]),
        },
    }


_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])|(?<=\.)\s")
_CJK = re.compile(r"[㐀-鿿]")


def _salient_title(chapter_segments: list[Segment]) -> str:
    def weight(text: str) -> int:
        # A CJK character carries roughly a word of meaning.
        return len(_CJK.findall(text)) + len(re.findall(r"[A-Za-z0-9]+", text))

    for segment in chapter_segments:
        for sentence in _SENTENCE_END.split(segment.text.strip()):
            sentence = sentence.strip()
            if weight(sentence) >= 4:
                return _clip(sentence)
    return _clip(chapter_segments[0].text.strip())


def _clip(text: str, limit: int = 40) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def time_slice_outline(
    transcript: AsrTranscript,
    *,
    duration_ms: int | None = None,
    window_ms: int = 300_000,
) -> dict:
    """Deterministic, model-free outline: fixed windows titled by first salient sentence."""
    segments = _segments(transcript)
    end_ms = _end_ms(segments, duration_ms)
    window_ms = max(window_ms, math.ceil(end_ms / MAX_CHAPTERS))
    groups: list[list[Segment]] = []
    boundary = window_ms
    for segment in segments:
        if not groups or _start_ms(segment) >= boundary:
            groups.append([])
            while _start_ms(segment) >= boundary:
                boundary += window_ms
        groups[-1].append(segment)
    starts = []
    index = 0
    for group in groups:
        starts.append((index, _salient_title(group)))
        index += len(group)
    chapters = _chapters(starts, segments, end_ms, None)
    validate_chapters(chapters, end_ms)
    return {
        "chapters": chapters,
        "method": "time_slices",
        "source": "local",
        "provider": TIME_SLICES_PROVIDER,
        "model": TIME_SLICES_MODEL,
        "prompt_version": TIME_SLICES_VERSION,
        "language": transcript.language,
        "detail": {
            "strategy": "time_slices",
            "window_ms": window_ms,
            "segments": len(segments),
            "chapter_count": len(chapters),
            "covered_until_ms": chapters[-1]["end_ms"],
            "last_segment_start_ms": _start_ms(segments[-1]),
            "map_calls": 0,
            "reduce_calls": 0,
        },
    }


def chapter_at(chapters: list[dict], position_ms: int) -> dict | None:
    """Return the chapter containing ``position_ms`` (for players and exports)."""
    current = None
    for chapter in chapters:
        if chapter["start_ms"] <= position_ms:
            current = chapter
        else:
            break
    return current
