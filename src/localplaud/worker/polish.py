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

PROMPT_VERSION = "transcript-polish/v4"
SYSTEM_PROMPT = """You polish ASR transcript segments for downstream notes.
Actively correct recognition errors using dialogue context and speaker continuity.
ASR spelling is not authoritative: preserving a name means preserving its intended
referent, not copying an obvious misrecognition. Resolve homophones, wrong word
boundaries, and technical terms when pronunciation and nearby dialogue strongly
support one reading. In Taiwan campus activity context, for example, a welcome
party misrecognized as「銀心派對」should be「迎新派對」. In astronomy context,
「銀心」can be correct and must remain. These are contextual examples, not global
replacement rules. Apply the same reasoning to other words, not only the examples.
Use repeated mentions and clear self-corrections in the supplied dialogue to keep
terminology consistent. Do not substitute a merely plausible person, song, brand,
or acronym without supporting context. Distinct titles must stay distinct even
when their spellings are similar. Where multiple readings remain plausible,
retain the source wording; do not manufacture certainty or explanatory annotations.
This is lexical correction, not rewriting: preserve the speaker's factual claims
even if your world knowledge disagrees. Do not replace unclear substantive words
with ellipses, delete questions, or complete broken utterances across segment or
speaker boundaries. Never turn a fragment into an acknowledgement or answer.
Prefer minimal spelling and punctuation edits; remove only obvious within-segment
stutters or accidental duplicates while preserving meaning,
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


def _propose_corrections(
    transcript: Transcript,
    settings: Settings,
    *,
    progress: Callable[[dict], None] | None = None,
    provider=None,
) -> dict:
    """Return a corrected copy with identical segment/timestamp/speaker structure."""
    from .transcript_quality import require_usable_transcript

    quality = require_usable_transcript(transcript)
    provider = provider or build_llm(settings.llm)
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
    kept_invalid = 0
    remapped_chunks = 0
    single_segment_retries: dict[int, int] = {}
    last_split_reason: str | None = None
    output_chars = 0
    request_input_chars = 0
    response_output_chars = 0
    skipped_empty_segments = sum(not str(segment.get("text") or "").strip() for segment in source)
    chunk_chars = getattr(provider, "polish_chunk_chars", settings.pipeline.polish_chunk_chars)
    if (
        isinstance(chunk_chars, bool)
        or not isinstance(chunk_chars, int)
        or not 1_000 <= chunk_chars <= 60_000
    ):
        raise LLMError("transcript polish chunk budget must be between 1000 and 60000")
    pending = list(_chunks(source, chunk_chars))
    target_segments_total = sum(bool(str(segment.get("text") or "").strip()) for segment in source)
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
            index for index in range(start, end) if str(source[index].get("text") or "").strip()
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
        # The output is roughly the corrected input text plus JSON scaffolding.
        # CJK text can reach ~2 tokens per character, so a flat budget truncates
        # long chunks mid-string and the response fails to parse.
        target_chars = sum(len(str(item["text"] or "")) for item in targets)
        try:
            raw_response = provider.complete(
                request_json,
                system=SYSTEM_PROMPT,
                temperature=0.1,
                max_tokens=max(2048, len(targets) * 80 + target_chars * 2),
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
                # Local models sometimes ignore the given IDs and renumber the
                # segments from 0 or 1. When the response is a complete,
                # in-order renumbering, map it back positionally instead of
                # discarding otherwise valid corrections.
                returned_ids = [item["id"] for item in returned]
                if len(returned_ids) == len(target_indexes) and returned_ids in (
                    list(range(len(returned_ids))),
                    list(range(1, len(returned_ids) + 1)),
                ):
                    by_id = {
                        index: by_id[given]
                        for index, given in zip(target_indexes, returned_ids, strict=True)
                    }
                    remapped_chunks += 1
                else:
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
                if single_segment_retries.get(start, 0) < 1:
                    single_segment_retries[start] = 1
                    pending[0:0] = [(start, end)]
                    split_retries += 1
                    last_split_reason = str(exc)
                    report_progress()
                    continue
                # One segment the model cannot return validly must not fail the
                # whole stage: keep the original timed text (already present in
                # ``polished``) and move on, recording the degradation.
                kept_source += len(target_indexes)
                kept_invalid += len(target_indexes)
                last_split_reason = str(exc)
                target_segments_completed += len(target_indexes)
                report_progress()
                continue
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
            "transcript_quality": quality,
            "chunk_chars": chunk_chars,
            "chunks": calls,
            "attempts": attempts,
            "split_retries": split_retries,
            "kept_source_segments": kept_source,
            "kept_missing_segments": kept_missing,
            "kept_emptied_segments": kept_emptied,
            "kept_invalid_segments": kept_invalid,
            "remapped_renumbered_chunks": remapped_chunks,
            "last_split_reason": last_split_reason,
            "skipped_empty_segments": skipped_empty_segments,
            "segments": len(source),
            "changed_segment_ids": [
                index
                for index, item in enumerate(polished)
                if item.get("text") != source[index].get("text")
            ],
            "input_chars": len(transcript.text),
            "output_chars": output_chars,
            "request_input_chars": request_input_chars,
            "response_output_chars": response_output_chars,
        },
    }


def polish_transcript(
    transcript: Transcript, settings: Settings, *, progress=None, dispatch_guard=None
) -> dict:
    """Generate, independently review, and publish only approved contextual edits."""
    from .correction_review import review_corrections

    provider = build_llm(settings.llm)
    if dispatch_guard is not None:

        class BudgetedProvider:
            def __getattr__(self, name):
                return getattr(self.inner, name)

            def complete(self, prompt, **kwargs):
                dispatch_guard(
                    {
                        "input_chars": len(prompt) + len(kwargs.get("system") or ""),
                        "output_tokens": kwargs.get("max_tokens", 2048),
                        "projection": True,
                    }
                )
                return self.inner.complete(prompt, **kwargs)

        guarded = BudgetedProvider()
        guarded.inner = provider
        provider = guarded
    result = _propose_corrections(transcript, settings, progress=progress, provider=provider)
    review = review_corrections(
        transcript,
        result["transcript"],
        provider,
        budget=result["detail"]["chunk_chars"],
        progress=progress,
    )
    detail = result["detail"]
    detail["review"] = review
    detail["strategy"] = "contextual-propose-review"
    detail["attempts"] += review["calls"]
    detail["request_input_chars"] += review["input_chars"]
    detail["response_output_chars"] += review["output_chars"]
    detail["changed_segment_ids"] = [
        i
        for i, (before, after) in enumerate(
            zip(transcript.segments, result["transcript"].segments, strict=True)
        )
        if before.text != after.text
    ]
    detail["output_chars"] = len(result["transcript"].text)
    return result
