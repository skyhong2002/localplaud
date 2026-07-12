"""Reference-based quality metrics for user-owned recordings."""

from __future__ import annotations

import json
import unicodedata
from functools import cache
from pathlib import Path

from sqlalchemy import select

from .db.models import PlaudFile, StageAttempt, StageName, StageStatus
from .db.session import session_scope
from .export_formats import recording_data

REFERENCE_SCHEMA = "localplaud-benchmark-reference/v1"
REPORT_SCHEMA = "localplaud-benchmark-report/v1"
SUITE_MANIFEST_SCHEMA = "localplaud-benchmark-suite/v1"
SUITE_REPORT_SCHEMA = "localplaud-benchmark-suite-report/v1"

SUITE_THRESHOLD_PATHS = {
    "cer": ("accuracy", "cer"),
    "wer": ("accuracy", "wer"),
    "der": ("speakers", "der"),
    "speech_character_insertion_rate": (
        "hallucination",
        "speech_character_insertion_rate",
    ),
    "speech_word_insertion_rate": ("hallucination", "speech_word_insertion_rate"),
    "non_speech_character_rate": ("hallucination", "non_speech_character_rate"),
    "boundary_mae_seconds": ("timestamps", "boundary_mae_seconds"),
    "real_time_factor": ("execution", "real_time_factor"),
    "peak_memory_mb": ("execution", "peak_memory_mb"),
}


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _edit_breakdown(reference: list[str], hypothesis: list[str]) -> dict:
    """Return deterministic Levenshtein counts with linear working memory."""
    # Each cell is cost, substitutions, deletions, insertions. Ties prefer a
    # substitution, then deletion, then insertion, matching the old backtrace.
    previous = [(offset, 0, 0, offset) for offset in range(len(hypothesis) + 1)]
    for index, expected in enumerate(reference, 1):
        current = [(index, 0, index, 0)]
        for offset, actual in enumerate(hypothesis, 1):
            if expected == actual:
                current.append(previous[offset - 1])
                continue
            diagonal = previous[offset - 1]
            above = previous[offset]
            left = current[offset - 1]
            candidates = (
                ((diagonal[0] + 1, diagonal[1] + 1, diagonal[2], diagonal[3]), 0),
                ((above[0] + 1, above[1], above[2] + 1, above[3]), 1),
                ((left[0] + 1, left[1], left[2], left[3] + 1), 2),
            )
            current.append(
                min(candidates, key=lambda candidate: (candidate[0][0], candidate[1]))[0]
            )
        previous = current
    errors, substitutions, deletions, insertions = previous[-1]
    return {
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "errors": errors,
        "reference_units": len(reference),
        "hypothesis_units": len(hypothesis),
        "error_rate": (errors / len(reference) if reference else None),
        "insertion_rate": insertions / len(reference) if reference else None,
    }


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

        @cache
        def assign(index: int, used: int) -> tuple[float, tuple[str | None, ...]]:
            if index == len(hypotheses):
                return 0.0, ()
            tail_score, tail = assign(index + 1, used)
            best = (tail_score, (None, *tail))
            for ref_index, ref in enumerate(references):
                if used & (1 << ref_index):
                    continue
                score, candidate_tail = assign(index + 1, used | (1 << ref_index))
                candidate = (
                    score + overlap.get((hypotheses[index], ref), 0.0),
                    (ref, *candidate_tail),
                )
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
        {float(item.get(key) or 0) for item in reference + hypothesis for key in ("start", "end")}
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


def _reference_insertion_metrics(
    expected_chars: list[str],
    actual_chars: list[str],
    expected_words: list[str],
    actual_words: list[str],
) -> dict:
    characters = _edit_breakdown(expected_chars, actual_chars)
    words = _edit_breakdown(expected_words, actual_words)
    return {
        "speech_character_insertions": characters["insertions"],
        "speech_character_insertion_rate": characters["insertion_rate"],
        "speech_word_insertions": words["insertions"],
        "speech_word_insertion_rate": words["insertion_rate"],
        "interpretation": "reference_aligned_asr_insertions_not_semantic_judgment",
    }


def load_reference(path: str | Path) -> dict:
    return validate_reference(json.loads(Path(path).read_text(encoding="utf-8")))


def load_suite_manifest(path: str | Path) -> tuple[dict, Path]:
    manifest_path = Path(path)
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return validate_suite_manifest(data), manifest_path.resolve().parent


def validate_suite_manifest(data: object) -> dict:
    if not isinstance(data, dict) or data.get("schema") != SUITE_MANIFEST_SCHEMA:
        raise ValueError(f"suite schema must be {SUITE_MANIFEST_SCHEMA}")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("suite must contain at least one case")
    seen: set[str] = set()
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            raise ValueError("every suite case must be an object")
        case_id = str(case.get("id") or f"case-{index}")
        if case_id in seen:
            raise ValueError(f"duplicate suite case id: {case_id}")
        seen.add(case_id)
        if not str(case.get("file_id") or "").strip():
            raise ValueError(f"suite case {case_id} requires file_id")
        if not str(case.get("reference") or "").strip():
            raise ValueError(f"suite case {case_id} requires reference")
    thresholds = data.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        raise ValueError("suite thresholds must be an object")
    unknown = sorted(set(thresholds) - set(SUITE_THRESHOLD_PATHS))
    if unknown:
        raise ValueError(f"unknown suite thresholds: {', '.join(unknown)}")
    for name, value in thresholds.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"suite threshold {name} must be a non-negative number")
    return data


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
    character_errors = _edit_breakdown(expected_chars, actual_chars)
    word_errors = _edit_breakdown(expected_words, actual_words)

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
            "cer": character_errors["error_rate"],
            "wer": word_errors["error_rate"],
            "reference_characters": len(expected_chars),
            "reference_words": len(expected_words),
            "character_errors": character_errors,
            "word_errors": word_errors,
        },
        "speakers": _speaker_metrics(expected, segments),
        "timestamps": _timestamp_metrics(expected, segments),
        "hallucination": _hallucination_metrics(reference, segments)
        | _reference_insertion_metrics(expected_chars, actual_chars, expected_words, actual_words),
        "execution": execution,
    }


def _suite_aggregates(reports: list[dict]) -> dict:
    def ratio(numerator, denominator):
        return round(numerator / denominator, 6) if denominator else None

    character_errors = sum(item["accuracy"]["character_errors"]["errors"] for item in reports)
    reference_characters = sum(item["accuracy"]["reference_characters"] for item in reports)
    word_errors = sum(item["accuracy"]["word_errors"]["errors"] for item in reports)
    reference_words = sum(item["accuracy"]["reference_words"] for item in reports)
    speaker_errors = sum(
        item["speakers"]["miss_seconds"]
        + item["speakers"]["false_alarm_seconds"]
        + item["speakers"]["confusion_seconds"]
        for item in reports
    )
    reference_speaker_seconds = sum(
        item["speakers"]["reference_speech_seconds"] for item in reports
    )
    character_insertions = sum(
        item["accuracy"]["character_errors"]["insertions"] for item in reports
    )
    word_insertions = sum(item["accuracy"]["word_errors"]["insertions"] for item in reports)
    non_speech_reports = [
        item
        for item in reports
        if item["hallucination"]["estimated_non_speech_characters"] is not None
    ]
    non_speech_characters = sum(
        item["hallucination"]["estimated_non_speech_characters"] for item in non_speech_reports
    )
    hypothesis_characters = sum(
        item["hallucination"]["hypothesis_characters"] for item in non_speech_reports
    )
    timestamp_reports = [
        item
        for item in reports
        if item["timestamps"]["boundary_mae_seconds"] is not None
        and item["timestamps"]["paired_segments"]
    ]
    paired_segments = sum(item["timestamps"]["paired_segments"] for item in timestamp_reports)
    boundary_error = sum(
        item["timestamps"]["boundary_mae_seconds"] * item["timestamps"]["paired_segments"]
        for item in timestamp_reports
    )
    execution_reports = [
        item
        for item in reports
        if item["execution"]["latency_seconds"] is not None and item["execution"]["audio_seconds"]
    ]
    latency_seconds = sum(item["execution"]["latency_seconds"] for item in execution_reports)
    audio_seconds = sum(item["execution"]["audio_seconds"] for item in execution_reports)
    memory = [
        item["execution"]["peak_memory_mb"]
        for item in reports
        if item["execution"]["peak_memory_mb"] is not None
    ]
    return {
        "recordings": len(reports),
        "cer": ratio(character_errors, reference_characters),
        "wer": ratio(word_errors, reference_words),
        "der": ratio(speaker_errors, reference_speaker_seconds),
        "speech_character_insertion_rate": ratio(character_insertions, reference_characters),
        "speech_word_insertion_rate": ratio(word_insertions, reference_words),
        "non_speech_character_rate": ratio(non_speech_characters, hypothesis_characters),
        "boundary_mae_seconds": ratio(boundary_error, paired_segments),
        "real_time_factor": ratio(latency_seconds, audio_seconds),
        "peak_memory_mb": max(memory) if memory else None,
        "coverage": {
            "reference_characters": reference_characters,
            "reference_words": reference_words,
            "reference_speaker_seconds": round(reference_speaker_seconds, 3),
            "full_audio_recordings": len(non_speech_reports),
            "timestamp_paired_recordings": len(timestamp_reports),
            "execution_timed_recordings": len(execution_reports),
        },
    }


def benchmark_suite(manifest: dict, base_dir: str | Path) -> dict:
    manifest = validate_suite_manifest(manifest)
    base = Path(base_dir)
    cases: list[dict] = []
    reports: list[dict] = []
    for index, item in enumerate(manifest["cases"], 1):
        case_id = str(item.get("id") or f"case-{index}")
        file_id = str(item["file_id"])
        reference_path = Path(str(item["reference"]))
        if not reference_path.is_absolute():
            reference_path = base / reference_path
        try:
            reference = load_reference(reference_path)
        except (OSError, ValueError, json.JSONDecodeError):
            cases.append(
                {
                    "id": case_id,
                    "file_id": file_id,
                    "status": "error",
                    "error": "reference could not be loaded or validated",
                }
            )
            continue
        try:
            report = benchmark_recording(file_id, reference)
        except (LookupError, ValueError):
            cases.append(
                {
                    "id": case_id,
                    "file_id": file_id,
                    "status": "error",
                    "error": "recording could not be benchmarked",
                }
            )
            continue
        reports.append(report)
        cases.append({"id": case_id, "file_id": file_id, "status": "completed", "report": report})

    aggregates = _suite_aggregates(reports)
    threshold_results = []
    for name, maximum in (manifest.get("thresholds") or {}).items():
        actual = aggregates[name]
        threshold_results.append(
            {
                "metric": name,
                "maximum": maximum,
                "actual": actual,
                "passed": actual is not None and actual <= maximum,
            }
        )
    return {
        "schema": SUITE_REPORT_SCHEMA,
        "suite": str(manifest.get("name") or "Private benchmark suite"),
        "target": manifest.get("target"),
        "passed": len(reports) == len(cases) and all(item["passed"] for item in threshold_results),
        "case_counts": {
            "total": len(cases),
            "completed": len(reports),
            "errors": len(cases) - len(reports),
        },
        "aggregates": aggregates,
        "thresholds": threshold_results,
        "cases": cases,
    }
