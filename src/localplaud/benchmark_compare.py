"""Compare speech benchmark runs: candidate vs baseline, and host A vs host B.

Two questions use the same cohort:

* **Default change.** Is a candidate configuration not worse than the current
  baseline on CER/MER, DER, timestamps, hallucination indicators and speed?
  ``compare_reports`` reads two ``benchmark-speech`` JSON reports.
* **Cross-host contract** (product-workflow acceptance scenario 8). Do two hosts,
  such as the Apple Silicon controller and the CUDA worker, produce comparable
  timestamped, diarized artifacts for the same audio? ``compare_hypotheses``
  reads the directories written by ``benchmark-speech --save-transcripts``.

Gate values are proposed review thresholds, not measured quality claims. Only
cohorts made entirely of owned recordings can support a production default; a
report containing public or synthetic audio is labelled as tooling evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

from .benchmark_speech import (
    HYPOTHESIS_SCHEMA,
    aggregate,
    diarization_error,
    text_metrics,
    timestamp_deviation,
)

REPORT_SCHEMA = "localplaud-speech-benchmark/v1"
COMPARISON_SCHEMA = "localplaud-speech-comparison/v1"

DEFAULT_GATES = {
    # Candidate vs baseline, absolute differences on the shared recordings.
    "max_cer_increase": 0.005,
    "max_mer_increase": 0.005,
    "max_der_increase": 0.01,
    "max_timestamp_mae_increase_seconds": 0.05,
    "max_new_hallucination_recordings": 0,
    "max_recording_cer_increase": 0.03,
    "max_rtf_ratio": 1.5,
    # Host A vs host B on identical audio.
    "max_cross_host_cer": 0.05,
    "max_cross_host_timestamp_mae_seconds": 0.25,
    "max_cross_host_speaker_disagreement": 0.10,
    "min_word_timing_coverage": 0.95,
    "min_speaker_coverage": 0.95,
}


def _load(path: Path, schema: str) -> dict:
    payload = json.loads(path.read_text())
    if payload.get("schema") != schema:
        raise ValueError(f"{path} is not a {schema} file")
    return payload


def _rate(metrics: dict, name: str) -> float | None:
    value = metrics.get(name)
    return value.get("rate") if isinstance(value, dict) else None


def _delta(new, old):
    return None if new is None or old is None else new - old


def compare_reports(baseline: dict, candidate: dict, gates: dict | None = None) -> dict:
    gates = DEFAULT_GATES | (gates or {})
    for report in (baseline, candidate):
        if report.get("schema") != REPORT_SCHEMA:
            raise ValueError("both inputs must be benchmark-speech reports")
    base = {r["id"]: r for r in baseline["recordings"] if r.get("metrics")}
    cand = {r["id"]: r for r in candidate["recordings"] if r.get("metrics")}
    shared = sorted(base.keys() & cand.keys())
    if not shared:
        raise ValueError("the reports share no successfully evaluated recording")
    a = aggregate([base[i] for i in shared])
    b = aggregate([cand[i] for i in shared])
    def agg_rate(report, name):
        return (report.get(name) or {}).get("rate")

    folded = agg_rate(a, "cer_script_folded") is not None and agg_rate(b, "cer_script_folded") is not None
    deltas = {
        "cer": _delta(b["cer"]["rate"], a["cer"]["rate"]),
        "cer_script_folded": _delta(agg_rate(b, "cer_script_folded"), agg_rate(a, "cer_script_folded")),
        "mer": _delta(b["mer"]["rate"], a["mer"]["rate"]),
        "der": _delta(b["der"]["rate"], a["der"]["rate"]),
        "timestamp_mae_seconds": _delta(b["timestamps"]["mean_absolute_seconds"],
                                        a["timestamps"]["mean_absolute_seconds"]),
        "hallucination_recordings": (
            b["hallucination"]["recordings_with_loops"] + b["hallucination"]["recordings_with_non_speech_text"]
            - a["hallucination"]["recordings_with_loops"] - a["hallucination"]["recordings_with_non_speech_text"]),
        "rtf_ratio": (b["real_time_factor"] / a["real_time_factor"]
                      if a["real_time_factor"] and b["real_time_factor"] is not None else None),
        "peak_memory_bytes": _delta(b["peak_memory_bytes"], a["peak_memory_bytes"]),
    }
    recordings = []
    for identity in shared:
        old, new = base[identity]["metrics"], cand[identity]["metrics"]
        cer_name = "cer_script_folded" if folded else "cer"
        recordings.append({
            "id": identity,
            "cer_delta": _delta(_rate(new, cer_name), _rate(old, cer_name)),
            "der_delta": _delta((new.get("der") or {}).get("rate"), (old.get("der") or {}).get("rate")),
            "new_loops": len(new["hallucination"]["repeated_ngram_loops"])
            - len(old["hallucination"]["repeated_ngram_loops"]),
        })
    failures = []

    def over(name, value, limit):
        if value is not None and value > limit:
            failures.append(f"{name} {value:+.4f} exceeds {limit}")

    # Gate on recognition, not script choice, whenever both reports carry folded CER.
    over("cer increase", deltas["cer_script_folded"] if folded else deltas["cer"], gates["max_cer_increase"])
    over("mer increase", deltas["mer"], gates["max_mer_increase"])
    over("der increase", deltas["der"], gates["max_der_increase"])
    over("timestamp MAE increase", deltas["timestamp_mae_seconds"], gates["max_timestamp_mae_increase_seconds"])
    over("new hallucination recordings", deltas["hallucination_recordings"], gates["max_new_hallucination_recordings"])
    over("RTF ratio", deltas["rtf_ratio"], gates["max_rtf_ratio"])
    for row in recordings:
        over(f"{row['id']} CER increase", row["cer_delta"], gates["max_recording_cer_increase"])
    sources = sorted({base[i].get("source", "owned") for i in shared} | {cand[i].get("source", "owned") for i in shared})
    evidence = "owned" if sources == ["owned"] else "tooling-only"
    return {
        "kind": "candidate-vs-baseline",
        "cer_gate": "cer_script_folded" if folded else "cer",
        "verdict": "regression" if failures else "not-worse",
        "evidence": evidence,
        "failures": failures,
        "shared_recordings": shared,
        "only_in_baseline": sorted(set(r["id"] for r in baseline["recordings"]) - set(shared)),
        "only_in_candidate": sorted(set(r["id"] for r in candidate["recordings"]) - set(shared)),
        "baseline": {"configuration": baseline.get("configuration"), "aggregate": a},
        "candidate": {"configuration": candidate.get("configuration"), "aggregate": b},
        "deltas": deltas,
        "recordings": recordings,
    }


def _words(payload: dict) -> list[dict]:
    words = []
    for segment in payload["segments"]:
        for word in segment.get("words") or []:
            words.append(word | {"speaker": word.get("speaker") or segment.get("speaker")})
    return words


def _speaker_turns(payload: dict) -> list[dict]:
    items = _words(payload) or payload["segments"]
    return [{"start": i["start"], "end": i["end"], "speaker": i["speaker"]}
            for i in items if i.get("speaker") and i["end"] > i["start"]]


def artifact_contract(payload: dict) -> dict:
    """Shape checks that every host's artifact must satisfy, independent of the other."""
    segments = payload["segments"]
    words = _words(payload)
    timed = [w for w in words if isinstance(w.get("start"), int | float) and isinstance(w.get("end"), int | float)
             and w["end"] >= w["start"]]
    text_segments = [s for s in segments if str(s.get("text") or "").strip()]
    with_speaker = [w for w in words if w.get("speaker")] if words else [s for s in text_segments if s.get("speaker")]
    starts = [s["start"] for s in segments]
    duration = payload.get("duration")
    return {
        "segments": len(segments),
        "words": len(words),
        "word_timing_coverage": len(timed) / len(words) if words else 0.0,
        "speaker_coverage": len(with_speaker) / (len(words) or len(text_segments)) if (words or text_segments) else 0.0,
        "speakers": len({i.get("speaker") for i in with_speaker}),
        "monotonic_segments": all(b >= a for a, b in zip(starts, starts[1:], strict=False)),
        "within_duration": duration is None or all(s["end"] <= duration + 0.5 for s in segments),
        "last_end_seconds": max((s["end"] for s in segments), default=0.0),
        "provider": payload.get("provider"),
        "model": payload.get("model"),
    }


def _judge(problems: list[str]):
    def judge(label, value, limit):
        if value is None:
            problems.append(f"{label} unavailable")
        elif value > limit:
            problems.append(f"{label} {value:.3f} exceeds {limit}")
    return judge


def compare_hypotheses(directory_a: Path, directory_b: Path, gates: dict | None = None) -> dict:
    gates = DEFAULT_GATES | (gates or {})
    a_files = {p.stem: p for p in directory_a.glob("*.json")}
    b_files = {p.stem: p for p in directory_b.glob("*.json")}
    shared = sorted(a_files.keys() & b_files.keys())
    if not shared:
        raise ValueError("the hypothesis directories share no recording")
    rows, failures = [], []
    for stem in shared:
        a = _load(a_files[stem], HYPOTHESIS_SCHEMA)
        b = _load(b_files[stem], HYPOTHESIS_SCHEMA)
        identity = a["id"]
        contract = {"a": artifact_contract(a), "b": artifact_contract(b)}
        a_text = "\n".join(s["text"] for s in a["segments"])
        b_text = "\n".join(s["text"] for s in b["segments"])
        cer = text_metrics(a_text, b_text)["cer_script_folded"]["rate"]
        a_words, b_words = _words(a), _words(b)
        timing = timestamp_deviation(a_words or a["segments"], b_words if a_words else b["segments"])
        a_turns, b_turns = _speaker_turns(a), _speaker_turns(b)
        speakers = diarization_error(a_turns, b_turns) if a_turns else None
        row = {"id": identity, "contract": contract, "cross_host_cer": cer,
               "timestamp_mae_seconds": timing["mean_absolute_seconds"],
               "timestamp_matched_fraction": timing["matched_units"] / timing["reference_units"]
               if timing["reference_units"] else None,
               "speaker_disagreement": speakers["rate"] if speakers else None,
               "speaker_mapping": speakers["mapping"] if speakers else None,
               "last_end_difference_seconds": abs(contract["a"]["last_end_seconds"] - contract["b"]["last_end_seconds"])}
        problems = []
        for side in ("a", "b"):
            c = contract[side]
            if c["word_timing_coverage"] < gates["min_word_timing_coverage"]:
                problems.append(f"host {side.upper()} word timing coverage {c['word_timing_coverage']:.3f}")
            if c["speaker_coverage"] < gates["min_speaker_coverage"]:
                problems.append(f"host {side.upper()} speaker coverage {c['speaker_coverage']:.3f}")
            if not c["monotonic_segments"] or not c["within_duration"]:
                problems.append(f"host {side.upper()} has out-of-order or out-of-range segments")
        judge = _judge(problems)
        judge("cross-host CER", cer, gates["max_cross_host_cer"])
        judge("cross-host timestamp MAE (s)", row["timestamp_mae_seconds"], gates["max_cross_host_timestamp_mae_seconds"])
        judge("speaker disagreement" if a_turns else "speaker disagreement (host A has no speaker turns)",
              row["speaker_disagreement"], gates["max_cross_host_speaker_disagreement"])
        if contract["a"]["speakers"] != contract["b"]["speakers"]:
            row["speaker_count_differs"] = True
        row["problems"] = problems
        failures.extend(f"{identity}: {p}" for p in problems)
        rows.append(row)
    return {"kind": "host-a-vs-host-b", "verdict": "diverged" if failures else "comparable",
            "failures": failures, "recordings": rows,
            "only_in_a": sorted(a_files.keys() - b_files.keys()),
            "only_in_b": sorted(b_files.keys() - a_files.keys())}


def compare(*, baseline: Path | None = None, candidate: Path | None = None,
            hypotheses_a: Path | None = None, hypotheses_b: Path | None = None,
            gates: dict | None = None) -> dict:
    if (baseline is None) != (candidate is None):
        raise ValueError("--baseline and --candidate go together")
    if (hypotheses_a is None) != (hypotheses_b is None):
        raise ValueError("--hypotheses-a and --hypotheses-b go together")
    if baseline is None and hypotheses_a is None:
        raise ValueError("give two reports, two hypothesis directories, or both")
    result = {"schema": COMPARISON_SCHEMA, "gates": DEFAULT_GATES | (gates or {})}
    if baseline is not None:
        result["reports"] = compare_reports(_load(baseline, REPORT_SCHEMA), _load(candidate, REPORT_SCHEMA), gates)
    if hypotheses_a is not None:
        result["hosts"] = compare_hypotheses(hypotheses_a, hypotheses_b, gates)
    result["passed"] = all(part["verdict"] in {"not-worse", "comparable"}
                           for part in (result.get("reports"), result.get("hosts")) if part)
    return result
