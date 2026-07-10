"""Privacy-safe speech quality acceptance benchmarks.

The benchmark deliberately evaluates pre-produced transcript artifacts.  It never
uploads, copies, or bundles source audio and its reports contain identifiers and
numbers only (not transcript text or filesystem paths).
"""

from __future__ import annotations

import json
import math
import re
from itertools import permutations
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BenchmarkSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = ""
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker: str | None = None

    @model_validator(mode="after")
    def valid_interval(self) -> BenchmarkSegment:
        if self.end < self.start:
            raise ValueError("segment end must not precede start")
        return self


class BenchmarkTranscript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[BenchmarkSegment]
    runtime_seconds: float | None = Field(default=None, gt=0)
    peak_memory_mb: float | None = Field(default=None, gt=0)
    audio_duration_seconds: float | None = Field(default=None, gt=0)


class AcceptanceThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_wer: float | None = Field(default=None, ge=0)
    max_cer: float | None = Field(default=None, ge=0)
    max_der: float | None = Field(default=None, ge=0)
    max_timestamp_mae_seconds: float | None = Field(default=None, ge=0)
    max_hallucination_rate: float | None = Field(default=None, ge=0)
    max_realtime_factor: float | None = Field(default=None, ge=0)
    max_peak_memory_mb: float | None = Field(default=None, ge=0)


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    language: str
    reference: str
    prediction: str
    consent: Literal[True]
    ownership: Literal["user-owned", "licensed-for-evaluation"]


class BenchmarkManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    dataset_name: str
    cases: list[BenchmarkCase]
    thresholds: AcceptanceThresholds = Field(default_factory=AcceptanceThresholds)

    @model_validator(mode="after")
    def unique_cases(self) -> BenchmarkManifest:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        if not ids:
            raise ValueError("at least one benchmark case is required")
        return self


def _local_path(base: Path, value: str) -> Path:
    """Resolve a manifest artifact without allowing a URI or directory escape."""
    if "://" in value or Path(value).is_absolute():
        raise ValueError("benchmark artifact paths must be local relative paths")
    base = base.resolve()
    result = (base / value).resolve()
    if not result.is_relative_to(base):
        raise ValueError("benchmark artifact path escapes the manifest directory")
    return result


def _tokens(text: str) -> list[str]:
    # Keep Han characters individually scoreable while retaining Latin words.
    return re.findall(r"[\u3400-\u9fff]|[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?", text.lower())


def _chars(text: str) -> list[str]:
    return [c.lower() for c in text if not c.isspace() and not re.match(r"[^\w\u3400-\u9fff]", c)]


def _edit_counts(reference: list[str], hypothesis: list[str]) -> tuple[int, int, int]:
    """Return substitutions, deletions and insertions from a minimal edit path."""
    rows: list[list[tuple[int, int, int, int]]] = [
        [(j, 0, 0, j) for j in range(len(hypothesis) + 1)]
    ]
    for i in range(1, len(reference) + 1):
        row = [(i, 0, i, 0)]
        for j in range(1, len(hypothesis) + 1):
            if reference[i - 1] == hypothesis[j - 1]:
                row.append(rows[i - 1][j - 1])
                continue
            candidates = (
                (rows[i - 1][j - 1][0] + 1, rows[i - 1][j - 1][1] + 1,
                 rows[i - 1][j - 1][2], rows[i - 1][j - 1][3]),
                (rows[i - 1][j][0] + 1, rows[i - 1][j][1],
                 rows[i - 1][j][2] + 1, rows[i - 1][j][3]),
                (row[j - 1][0] + 1, row[j - 1][1], row[j - 1][2],
                 row[j - 1][3] + 1),
            )
            row.append(min(candidates))
        rows.append(row)
    _, substitutions, deletions, insertions = rows[-1][-1]
    return substitutions, deletions, insertions


def _rate(reference: list[str], hypothesis: list[str]) -> tuple[float, float]:
    substitutions, deletions, insertions = _edit_counts(reference, hypothesis)
    denominator = max(1, len(reference))
    return (substitutions + deletions + insertions) / denominator, insertions / denominator


def _speaker_at(segments: list[BenchmarkSegment], point: float) -> str | None:
    for segment in segments:
        if segment.start <= point < segment.end and segment.speaker:
            return segment.speaker
    return None


def _der(reference: list[BenchmarkSegment], hypothesis: list[BenchmarkSegment]) -> float | None:
    """Collar-free DER approximation on the union of all segment boundaries.

    Hypothesis speaker labels are optimally mapped to reference labels. Overlapping
    speech is not representable by this compact manifest and is therefore excluded.
    """
    boundaries = sorted({x for s in reference + hypothesis for x in (s.start, s.end)})
    if len(boundaries) < 2 or not any(s.speaker for s in reference):
        return None
    intervals: list[tuple[float, str | None, str | None]] = []
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        if right <= left:
            continue
        middle = (left + right) / 2
        intervals.append((right - left, _speaker_at(reference, middle),
                          _speaker_at(hypothesis, middle)))
    ref_labels = sorted({r for _, r, _ in intervals if r})
    hyp_labels = sorted({h for _, _, h in intervals if h})
    # Weight the confusion matrix by duration, not interval count.
    weights: dict[tuple[str, str], float] = {}
    for duration, ref, hyp in intervals:
        if ref and hyp:
            weights[(ref, hyp)] = weights.get((ref, hyp), 0.0) + duration
    mapping: dict[str, str] = {}
    if len(ref_labels) <= 8 and len(hyp_labels) <= 8:
        padded = ref_labels + [""] * max(0, len(hyp_labels) - len(ref_labels))
        best = (-1.0, ())
        for assigned in permutations(padded, len(hyp_labels)):
            score = sum(
                weights.get((ref, hyp), 0)
                for hyp, ref in zip(hyp_labels, assigned, strict=True)
            )
            if score > best[0]:
                best = (score, assigned)
        mapping = dict(zip(hyp_labels, best[1], strict=True))
    else:
        for hyp in hyp_labels:
            mapping[hyp] = max(ref_labels, key=lambda ref: weights.get((ref, hyp), 0))
    scored = sum(duration for duration, ref, _ in intervals if ref)
    errors = sum(duration for duration, ref, hyp in intervals if ref and mapping.get(hyp or "") != ref)
    return errors / scored if scored else None


def _timestamp_mae(reference: list[BenchmarkSegment], hypothesis: list[BenchmarkSegment]) -> float | None:
    """Boundary MAE for positionally corresponding, non-empty segments."""
    pairs = [(r, h) for r, h in zip(reference, hypothesis, strict=False) if r.text and h.text]
    if not pairs:
        return None
    errors = [abs(a - b) for r, h in pairs for a, b in ((r.start, h.start), (r.end, h.end))]
    return sum(errors) / len(errors)


def evaluate_transcripts(reference: BenchmarkTranscript, prediction: BenchmarkTranscript) -> dict[str, float | None]:
    reference_text = " ".join(s.text for s in reference.segments)
    prediction_text = " ".join(s.text for s in prediction.segments)
    wer, hallucination_rate = _rate(_tokens(reference_text), _tokens(prediction_text))
    cer, _ = _rate(_chars(reference_text), _chars(prediction_text))
    duration = prediction.audio_duration_seconds or reference.audio_duration_seconds
    runtime = prediction.runtime_seconds
    return {
        "wer": wer,
        "cer": cer,
        "der": _der(reference.segments, prediction.segments),
        "timestamp_mae_seconds": _timestamp_mae(reference.segments, prediction.segments),
        "hallucination_rate": hallucination_rate,
        "runtime_seconds": runtime,
        "realtime_factor": runtime / duration if runtime is not None and duration else None,
        "peak_memory_mb": prediction.peak_memory_mb,
    }


def _aggregate(results: list[dict[str, Any]]) -> dict[str, float | None]:
    keys = results[0]["metrics"] if results else {}
    return {
        key: (sum(values) / len(values) if values else None)
        for key in keys
        for values in [[r["metrics"][key] for r in results if r["metrics"][key] is not None]]
    }


def run_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest = BenchmarkManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    results = []
    for case in manifest.cases:
        reference = BenchmarkTranscript.model_validate_json(
            _local_path(manifest_path.parent, case.reference).read_text(encoding="utf-8")
        )
        prediction = BenchmarkTranscript.model_validate_json(
            _local_path(manifest_path.parent, case.prediction).read_text(encoding="utf-8")
        )
        results.append({"id": case.id, "language": case.language,
                        "metrics": evaluate_transcripts(reference, prediction)})
    aggregate = _aggregate(results)
    failures = []
    threshold_map = {
        "max_wer": "wer", "max_cer": "cer", "max_der": "der",
        "max_timestamp_mae_seconds": "timestamp_mae_seconds",
        "max_hallucination_rate": "hallucination_rate",
        "max_realtime_factor": "realtime_factor", "max_peak_memory_mb": "peak_memory_mb",
    }
    for threshold_name, metric_name in threshold_map.items():
        limit = getattr(manifest.thresholds, threshold_name)
        if limit is None:
            continue
        for result in results:
            value = result["metrics"].get(metric_name)
            if value is None or not math.isfinite(value) or value > limit:
                failures.append({"case_id": result["id"], "metric": metric_name,
                                 "value": value, "maximum": limit})
    return {"schema_version": 1, "dataset_name": manifest.dataset_name,
            "privacy": "numeric-only; no transcript text or artifact paths",
            "cases": results, "aggregate": aggregate, "failures": failures,
            "passed": not failures}


def write_report(manifest_path: Path, output_path: Path) -> dict[str, Any]:
    report = run_manifest(manifest_path)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
