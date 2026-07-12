"""Reference-based quality metrics for user-owned recordings."""

from __future__ import annotations

import json
import unicodedata
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


def _speaker_at(segments: list[dict], moment: float) -> str | None:
    for item in segments:
        if float(item.get("start") or 0) <= moment < float(item.get("end") or 0):
            return item.get("speaker")
    return None


def _speaker_metrics(reference: list[dict], hypothesis: list[dict]) -> dict:
    boundaries = sorted(
        {
            float(item.get(key) or 0)
            for item in reference + hypothesis
            for key in ("start", "end")
        }
    )
    intervals = [
        (start, end, _speaker_at(reference, (start + end) / 2), _speaker_at(hypothesis, (start + end) / 2))
        for start, end in zip(boundaries, boundaries[1:], strict=False)
        if end > start
    ]
    overlap: dict[tuple[str, str], float] = {}
    for start, end, ref, hyp in intervals:
        if ref and hyp:
            overlap[(hyp, ref)] = overlap.get((hyp, ref), 0.0) + end - start
    mapping: dict[str, str] = {}
    used_ref: set[str] = set()
    for (hyp, ref), _duration in sorted(overlap.items(), key=lambda item: -item[1]):
        if hyp not in mapping and ref not in used_ref:
            mapping[hyp] = ref
            used_ref.add(ref)

    miss = false_alarm = confusion = reference_speech = 0.0
    for start, end, ref, hyp in intervals:
        duration = end - start
        if ref:
            reference_speech += duration
        if ref and not hyp:
            miss += duration
        elif hyp and not ref:
            false_alarm += duration
        elif ref and hyp and mapping.get(hyp) != ref:
            confusion += duration
    total_error = miss + false_alarm + confusion
    return {
        "der": total_error / reference_speech if reference_speech else None,
        "miss_seconds": round(miss, 3),
        "false_alarm_seconds": round(false_alarm, 3),
        "confusion_seconds": round(confusion, 3),
        "reference_speech_seconds": round(reference_speech, 3),
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


def load_reference(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
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
            "peak_memory_mb": None,
        }

    return {
        "schema": REPORT_SCHEMA,
        "file_id": file_id,
        "reference": {
            "schema": reference["schema"],
            "language": reference.get("language"),
            "case": reference.get("case"),
        },
        "accuracy": {
            "cer": _error_rate(expected_chars, actual_chars),
            "wer": _error_rate(expected_words, actual_words),
            "reference_characters": len(expected_chars),
            "reference_words": len(expected_words),
        },
        "speakers": _speaker_metrics(expected, segments),
        "timestamps": _timestamp_metrics(expected, segments),
        "execution": execution,
    }
