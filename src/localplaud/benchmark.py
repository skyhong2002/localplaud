"""Reference-based quality metrics for user-owned recordings."""

from __future__ import annotations

import json
import unicodedata
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select

from .db.models import PlaudFile, StageAttempt, StageName, StageStatus
from .db.session import session_scope
from .export_formats import recording_data

REFERENCE_SCHEMA = "localplaud-benchmark-reference/v1"
REPORT_SCHEMA = "localplaud-benchmark-report/v1"


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _edit_distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for index, token in enumerate(left, 1):
        current = [index]
        for offset, other in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[offset] + 1,
                    previous[offset - 1] + (token != other),
                )
            )
        previous = current
    return previous[-1]


def _error_rate(reference: list[str], hypothesis: list[str]) -> float | None:
    if not reference:
        return None
    return _edit_distance(reference, hypothesis) / len(reference)


def _active_speakers(segments: list[dict], moment: float) -> set[str]:
    return {
        speaker
        for item in segments
        if float(item.get("start") or 0) <= moment < float(item.get("end") or 0)
        if (speaker := item.get("speaker"))
    }


def _speaker_mapping(overlap: dict[tuple[str, str], float]) -> dict[str, str]:
    hypotheses = sorted({hyp for hyp, _ref in overlap})
    references = sorted({ref for _hyp, ref in overlap})

    # Typical meetings have few speakers. Use an exact maximum-overlap assignment
    # there and retain a bounded deterministic fallback for unusually large labels.
    if len(hypotheses) <= 12 and len(references) <= 12:
        @lru_cache(maxsize=None)
        def assign(index: int, used: int) -> tuple[float, tuple[str | None, ...]]:
            if index == len(hypotheses):
                return 0.0, ()
            tail_score, tail = assign(index + 1, used)
            best = (tail_score, (None, *tail))
            for ref_index, ref in enumerate(references):
                if used & (1 << ref_index):
                    continue
                score, candidate_tail = assign(index + 1, used | (1 << ref_index))
                candidate = (score + overlap.get((hypotheses[index], ref), 0.0), (ref, *candidate_tail))
                if candidate[0] > best[0]:
                    best = candidate
            return best

        _score, assignment = assign(0, 0)
        return {
            hyp: ref
            for hyp, ref in zip(hypotheses, assignment, strict=True)
            if ref is not None and overlap.get((hyp, ref), 0.0) > 0
        }

    mapping: dict[str, str] = {}
    used_ref: set[str] = set()
    for (hyp, ref), _duration in sorted(overlap.items(), key=lambda item: (-item[1], item[0])):
        if hyp not in mapping and ref not in used_ref:
            mapping[hyp] = ref
            used_ref.add(ref)
    return mapping


def _speaker_metrics(reference: list[dict], hypothesis: list[dict]) -> dict:
    boundaries = sorted(
        {
            float(item.get(key) or 0)
            for item in reference + hypothesis
            for key in ("start", "end")
        }
    )
    intervals = [
        (
            start,
            end,
            _active_speakers(reference, (start + end) / 2),
            _active_speakers(hypothesis, (start + end) / 2),
        )
        for start, end in zip(boundaries, boundaries[1:], strict=False)
        if end > start
    ]
    overlap: dict[tuple[str, str], float] = {}
    for start, end, refs, hyps in intervals:
        for hyp in hyps:
            for ref in refs:
                overlap[(hyp, ref)] = overlap.get((hyp, ref), 0.0) + end - start
    mapping = _speaker_mapping(overlap)

    miss = false_alarm = confusion = reference_speech = 0.0
    for start, end, refs, hyps in intervals:
        duration = end - start
        reference_speech += len(refs) * duration
        mapped_hyps = {mapping[hyp] for hyp in hyps if hyp in mapping}
        correct = len(refs & mapped_hyps)
        miss += max(0, len(refs) - len(hyps)) * duration
        false_alarm += max(0, len(hyps) - len(refs)) * duration
        confusion += (min(len(refs), len(hyps)) - correct) * duration
    total_error = miss + false_alarm + confusion
    return {
        "der": total_error / reference_speech if reference_speech else None,
        "miss_seconds": round(miss, 3),
        "false_alarm_seconds": round(false_alarm, 3),
        "confusion_seconds": round(confusion, 3),
        "reference_speech_seconds": round(reference_speech, 3),
        "overlap_aware": True,
        "speaker_mapping": mapping,
    }


def _timestamp_metrics(reference: list[dict], hypothesis: list[dict]) -> dict:
    if not reference or len(reference) != len(hypothesis):
        return {"boundary_mae_seconds": None, "paired_segments": 0}
    errors = []
    for expected, actual in zip(reference, hypothesis, strict=True):
        errors.extend(
            [
                abs(float(expected.get("start") or 0) - float(actual.get("start") or 0)),
                abs(float(expected.get("end") or 0) - float(actual.get("end") or 0)),
            ]
        )
    return {
        "boundary_mae_seconds": round(sum(errors) / len(errors), 4),
        "paired_segments": len(reference),
    }


def _interval_overlap(start: float, end: float, intervals: list[tuple[float, float]]) -> float:
    clipped = sorted(
        (max(start, left), min(end, right))
        for left, right in intervals
        if min(end, right) > max(start, left)
    )
    total = 0.0
    cursor_start = cursor_end = None
    for left, right in clipped:
        if cursor_start is None:
            cursor_start, cursor_end = left, right
        elif left <= cursor_end:
            cursor_end = max(cursor_end, right)
        else:
            total += cursor_end - cursor_start
            cursor_start, cursor_end = left, right
    if cursor_start is not None:
        total += cursor_end - cursor_start
    return total


def _hallucination_metrics(reference: dict, hypothesis: list[dict]) -> dict:
    if reference.get("coverage") != "full_audio":
        return {
            "non_speech_character_rate": None,
            "estimated_non_speech_characters": None,
            "hypothesis_characters": None,
            "majority_non_speech_segments": None,
            "reason": "reference coverage is not full_audio",
        }
    speech = [
        (float(item.get("start") or 0), float(item.get("end") or 0))
        for item in reference["segments"]
    ]
    total_characters = 0
    estimated_non_speech = 0.0
    majority_non_speech = 0
    for item in hypothesis:
        characters = len(_normalize(str(item.get("text") or "")).replace(" ", ""))
        total_characters += characters
        start, end = float(item.get("start") or 0), float(item.get("end") or 0)
        duration = max(0.0, end - start)
        speech_overlap = min(duration, _interval_overlap(start, end, speech))
        non_speech_fraction = 1.0 - speech_overlap / duration if duration else 0.0
        estimated_non_speech += characters * non_speech_fraction
        if characters and non_speech_fraction > 0.5:
            majority_non_speech += 1
    return {
        "non_speech_character_rate": (
            round(estimated_non_speech / total_characters, 6) if total_characters else None
        ),
        "estimated_non_speech_characters": round(estimated_non_speech, 3),
        "hypothesis_characters": total_characters,
        "majority_non_speech_segments": majority_non_speech,
        "reason": None,
    }


def load_reference(path: str | Path) -> dict:
    return validate_reference(json.loads(Path(path).read_text(encoding="utf-8")))


def validate_reference(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("reference must be a JSON object")
    if data.get("schema") != REFERENCE_SCHEMA:
        raise ValueError(f"reference schema must be {REFERENCE_SCHEMA}")
    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("reference must contain at least one segment")
    for item in segments:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            raise ValueError("every reference segment requires text")
        if item.get("start") is None or item.get("end") is None:
            raise ValueError("every reference segment requires start and end")
        if float(item["end"]) < float(item["start"]):
            raise ValueError("reference segment end must not precede start")
    return data


def benchmark_recording(file_id: str, reference: dict) -> dict:
    segments = list(recording_data(file_id)["segments"])
    if not segments:
        raise ValueError("recording has no local canonical transcript")
    expected = list(reference["segments"])
    expected_text = _normalize(reference.get("text") or " ".join(x["text"] for x in expected))
    actual_text = _normalize(" ".join(str(x.get("text") or "") for x in segments))
    expected_chars = list(expected_text.replace(" ", ""))
    actual_chars = list(actual_text.replace(" ", ""))
    expected_words = expected_text.split()
    actual_words = actual_text.split()

    with session_scope() as session:
        file = session.get(PlaudFile, file_id)
        if file is None:
            raise LookupError(f"recording not found: {file_id}")
        attempt = session.scalar(
            select(StageAttempt)
            .where(
                StageAttempt.file_id == file_id,
                StageAttempt.stage == StageName.transcribe,
                StageAttempt.status == StageStatus.completed,
            )
            .order_by(StageAttempt.attempt.desc())
        )
        audio_seconds = (file.duration_ms or 0) / 1000
        latency_seconds = (attempt.latency_ms or 0) / 1000 if attempt else None
        execution = {
            "provider": attempt.provider if attempt else None,
            "model": attempt.model if attempt else None,
            "latency_seconds": latency_seconds,
            "audio_seconds": audio_seconds or None,
            "real_time_factor": (
                round(latency_seconds / audio_seconds, 4)
                if latency_seconds is not None and audio_seconds
                else None
            ),
            "peak_memory_mb": (
                (attempt.usage or {}).get("process_peak_memory_mb") if attempt else None
            ),
            "memory_scope": "worker_process_high_water_rss",
        }

    return {
        "schema": REPORT_SCHEMA,
        "file_id": file_id,
        "reference": {
            "schema": reference["schema"],
            "language": reference.get("language"),
            "case": reference.get("case"),
            "coverage": reference.get("coverage"),
        },
        "accuracy": {
            "cer": _error_rate(expected_chars, actual_chars),
            "wer": _error_rate(expected_words, actual_words),
            "reference_characters": len(expected_chars),
            "reference_words": len(expected_words),
        },
        "speakers": _speaker_metrics(expected, segments),
        "timestamps": _timestamp_metrics(expected, segments),
        "hallucination": _hallucination_metrics(reference, segments),
        "execution": execution,
    }
