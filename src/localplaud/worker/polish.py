"""Context-aware transcript correction that preserves timing and speakers."""

from __future__ import annotations

import copy
import json
import re
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


def polish_transcript(transcript: Transcript, settings: Settings) -> dict:
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
                by_id[item["id"]] = item["text"].strip()
            expected = set(target_indexes)
            if set(by_id) != expected:
                raise LLMOutputInvalid("transcript polish changed or omitted segment IDs")
            emptied = [
                index
                for index in target_indexes
                if str(source[index].get("text") or "").strip() and not by_id[index]
            ]
            if emptied:
                filler_like = all(
                    len(str(source[index].get("text") or "").strip()) <= 4
                    for index in emptied
                )
                if not filler_like and end - start > 1:
                    raise LLMOutputInvalid(
                        "transcript polish emptied non-empty segment text"
                    )
                # The model legitimately empties filler-only segments ("呃",
                # "嗯", "!") that the prompt tells it to remove, and can insist
                # on emptying a lone segment even after splitting. Keep the
                # source text instead of failing the stage: correction must
                # never destroy transcript content.
                for index in emptied:
                    by_id[index] = str(source[index].get("text") or "").strip()
                kept_source += len(emptied)
        except LLMOutputInvalid:
            if end - start <= 1:
                raise
            midpoint = start + (end - start) // 2
            pending[0:0] = [(start, midpoint), (midpoint, end)]
            split_retries += 1
            continue
        for index in target_indexes:
            polished[index]["text"] = by_id[index]
            output_chars += len(by_id[index])
        calls += 1

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
            "skipped_empty_segments": skipped_empty_segments,
            "segments": len(source),
            "input_chars": len(transcript.text),
            "output_chars": output_chars,
            "request_input_chars": request_input_chars,
            "response_output_chars": response_output_chars,
        },
    }
