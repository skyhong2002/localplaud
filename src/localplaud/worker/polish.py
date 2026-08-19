"""Context-aware transcript correction that preserves timing and speakers."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import asdict

from ..asr.base import Segment, Transcript, Word
from ..config import Settings
from ..llm.base import LLMError, LLMOutputInvalid, build_llm

PROMPT_VERSION = "transcript-polish/v1"
SYSTEM_PROMPT = """You polish ASR transcript segments for downstream notes.
Correct recognition errors using dialogue context and speaker continuity. Remove
stutters, accidental repetitions, and non-semantic filler while preserving meaning,
uncertainty, tone, names, numbers, dates, decisions, negation, language switching,
segment IDs, and speaker ownership. Use Traditional Chinese (Taiwan) where Chinese
is present. Never summarize, invent, merge, split, or add commentary. Return only
JSON: {\"segments\":[{\"id\":integer,\"text\":string}, ...]} with exactly one
entry for every target segment and no context segments."""
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}


def _chunks(segments: list[dict], limit: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    size = 0
    for index, segment in enumerate(segments):
        cost = len(str(segment.get("text") or "")) + 80
        if index > start and size + cost > limit:
            ranges.append((start, index))
            start = index
            size = 0
        size += cost
    if start < len(segments):
        ranges.append((start, len(segments)))
    return ranges


def _json_completion(value: str) -> dict:
    cleaned = value.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL)
    if fenced:
        cleaned = fenced.group(1)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMOutputInvalid(f"transcript polish returned invalid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise LLMOutputInvalid("transcript polish response must be a JSON object")
    return result


def polish_transcript(
    transcript: Transcript,
    settings: Settings,
    *,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Return a corrected copy with identical segment/timestamp/speaker structure."""
    provider = build_llm(settings.llm)
    if not provider.available():
        raise LLMError(f"transcript polish provider unavailable: {provider.name}")
    source = [asdict(segment) for segment in transcript.segments]
    polished = copy.deepcopy(source)
    for item in polished:
        if not str(item.get("text") or "").strip():
            item["text"] = ""
    calls = 0
    attempts = 0
    split_retries = 0
    kept_source = 0
    kept_missing = 0
    kept_emptied = 0
    last_split_reason: str | None = None
    output_chars = 0
    request_input_chars = 0
    response_output_chars = 0
    skipped_empty_segments = sum(
        not str(segment.get("text") or "").strip() for segment in source
    )
    chunk_chars = getattr(provider, "polish_chunk_chars", settings.pipeline.polish_chunk_chars)
    if (
        isinstance(chunk_chars, bool)
        or not isinstance(chunk_chars, int)
        or not 1_000 <= chunk_chars <= 60_000
    ):
        raise LLMError("transcript polish chunk budget must be between 1000 and 60000")
    pending = list(_chunks(source, chunk_chars))
    target_segments_total = sum(
        bool(str(segment.get("text") or "").strip()) for segment in source
    )
    target_segments_completed = 0

    def report_progress() -> None:
        if progress is None:
            return
        progress(
            {
                "strategy": "contextual-segment-map",
                "segments": len(source),
                "target_segments_total": target_segments_total,
                "target_segments_completed": target_segments_completed,
                "chunks_completed": calls,
                "chunks_total": calls + len(pending),
                "attempts": attempts,
                "split_retries": split_retries,
                "kept_source_segments": kept_source,
                "last_split_reason": last_split_reason,
            }
        )

    report_progress()
    while pending:
        start, end = pending.pop(0)
        target_indexes = [
            index
            for index in range(start, end)
            if str(source[index].get("text") or "").strip()
        ]
        if not target_indexes:
            continue
        targets = [
            {
                "id": index,
                "speaker": source[index].get("speaker"),
                "text": source[index].get("text", ""),
            }
            for index in target_indexes
        ]
        request = {
            "language": transcript.language,
            "context_before": [
                {
                    "speaker": item.get("speaker"),
                    "text": item.get("text", ""),
                }
                for item in source[max(0, start - 2) : start]
            ],
            "target_segments": targets,
            "context_after": [
                {
                    "speaker": item.get("speaker"),
                    "text": item.get("text", ""),
                }
                for item in source[end : min(len(source), end + 2)]
            ],
        }
        request_json = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        attempts += 1
        request_input_chars += len(SYSTEM_PROMPT) + len(request_json)
        try:
            raw_response = provider.complete(
                request_json,
                system=SYSTEM_PROMPT,
                temperature=0.1,
                max_tokens=max(2048, len(targets) * 80),
                json_schema=RESPONSE_SCHEMA,
            )
            response_output_chars += len(raw_response)
            response = _json_completion(raw_response)
            returned = response.get("segments")
            if not isinstance(returned, list):
                raise LLMOutputInvalid("transcript polish response has no segments array")
            by_id: dict[int, str] = {}
            for item in returned:
                if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                    raise LLMOutputInvalid("transcript polish returned an invalid segment entry")
                if not isinstance(item.get("text"), str):
                    raise LLMOutputInvalid("transcript polish segment text must be a string")
                item_id = item["id"]
                if item_id in by_id:
                    raise LLMOutputInvalid("transcript polish returned duplicate segment IDs")
                by_id[item_id] = item["text"].strip()
            expected = set(target_indexes)
            unexpected = set(by_id) - expected
            if unexpected:
                raise LLMOutputInvalid("transcript polish returned unexpected segment IDs")
            # A local model can omit a segment while otherwise returning valid
            # corrections. Preserve those source segments instead of recursively
            # rerunning the whole chunk: omission must never lose transcript text,
            # and the successful corrections remain useful downstream.
            missing = expected - set(by_id)
            for index in missing:
                by_id[index] = str(source[index].get("text") or "").strip()
            kept_source += len(missing)
            kept_missing += len(missing)
            emptied = [
                index
                for index in target_indexes
                if str(source[index].get("text") or "").strip() and not by_id[index]
            ]
            if emptied:
                # The model can legitimately empty filler-only segments, but it
                # can also empty substantive text. In both cases the safest
                # degradation is the original timed segment, not an expensive
                # recursive retry that may repeat the same omission.
                for index in emptied:
                    by_id[index] = str(source[index].get("text") or "").strip()
                kept_source += len(emptied)
                kept_emptied += len(emptied)
        except LLMOutputInvalid as exc:
            if end - start <= 1:
                raise
            midpoint = start + (end - start) // 2
            pending[0:0] = [(start, midpoint), (midpoint, end)]
            split_retries += 1
            last_split_reason = str(exc)
            report_progress()
            continue
        for index in target_indexes:
            polished[index]["text"] = by_id[index]
            output_chars += len(by_id[index])
        calls += 1
        target_segments_completed += len(target_indexes)
        report_progress()

    result = Transcript(
        segments=[],
        language=transcript.language,
        duration=transcript.duration,
        provider=provider.name,
        model=getattr(provider, "model", None),
        has_speakers=transcript.has_speakers,
    )
    result.segments = [
        Segment(
            text=item.get("text", ""),
            start=item.get("start", 0.0),
            end=item.get("end", 0.0),
            speaker=item.get("speaker"),
            words=[Word(**word) for word in item.get("words", [])],
        )
        for item in polished
    ]
    return {
        "transcript": result,
        "provider": provider.name,
        "model": getattr(provider, "model", None),
        "prompt_version": PROMPT_VERSION,
        "detail": {
            "strategy": "contextual-segment-map",
            "chunk_chars": chunk_chars,
            "chunks": calls,
            "attempts": attempts,
            "split_retries": split_retries,
            "kept_source_segments": kept_source,
            "kept_missing_segments": kept_missing,
            "kept_emptied_segments": kept_emptied,
            "last_split_reason": last_split_reason,
            "skipped_empty_segments": skipped_empty_segments,
            "segments": len(source),
            "input_chars": len(transcript.text),
            "output_chars": output_chars,
            "request_input_chars": request_input_chars,
            "response_output_chars": response_output_chars,
        },
    }
