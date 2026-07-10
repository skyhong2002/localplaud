from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from localplaud.benchmark import BenchmarkTranscript, evaluate_transcripts, run_manifest
from localplaud.cli import app


def transcript(segments, **metadata):
    return BenchmarkTranscript.model_validate({"segments": segments, **metadata})


def test_metrics_cover_accuracy_speakers_timestamps_runtime_and_memory():
    reference = transcript(
        [
            {"text": "你好 team", "start": 0, "end": 2, "speaker": "alice"},
            {"text": "早安", "start": 2, "end": 4, "speaker": "bob"},
        ],
        audio_duration_seconds=10,
    )
    prediction = transcript(
        [
            {"text": "你好 team", "start": 0.2, "end": 2.2, "speaker": "S1"},
            {"text": "早安 extra", "start": 2.2, "end": 4.2, "speaker": "S2"},
        ],
        runtime_seconds=2,
        peak_memory_mb=512,
    )

    result = evaluate_transcripts(reference, prediction)

    assert result["wer"] == pytest.approx(1 / 5)
    assert result["hallucination_rate"] == pytest.approx(1 / 5)
    assert result["cer"] > 0
    assert result["der"] == pytest.approx(0.1)
    assert result["timestamp_mae_seconds"] == pytest.approx(0.2)
    assert result["realtime_factor"] == pytest.approx(0.2)
    assert result["peak_memory_mb"] == 512


def test_manifest_requires_explicit_consent_and_report_never_contains_content_or_paths(tmp_path):
    refs = tmp_path / "refs"
    refs.mkdir()
    artifact = {"segments": [{"text": "private words", "start": 0, "end": 1}]}
    (refs / "reference.json").write_text(json.dumps(artifact))
    (refs / "prediction.json").write_text(json.dumps(artifact))
    manifest = {
        "schema_version": 1,
        "dataset_name": "anonymous-suite",
        "cases": [{"id": "case-1", "language": "en", "reference": "refs/reference.json",
                   "prediction": "refs/prediction.json", "consent": True,
                   "ownership": "user-owned"}],
        "thresholds": {"max_wer": 0},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))

    report = run_manifest(path)
    serialized = json.dumps(report)

    assert report["passed"] is True
    assert "private words" not in serialized
    assert "reference.json" not in serialized
    manifest["cases"][0]["consent"] = False
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValidationError):
        run_manifest(path)


def test_manifest_rejects_remote_and_escaping_paths(tmp_path):
    base = {"schema_version": 1, "dataset_name": "x", "cases": [{"id": "x",
            "language": "zh-TW", "reference": "https://example.test/ref.json",
            "prediction": "../prediction.json", "consent": True,
            "ownership": "licensed-for-evaluation"}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(base))
    with pytest.raises(ValueError, match="local relative"):
        run_manifest(path)


def test_missing_threshold_metric_fails_closed_and_cli_returns_one(tmp_path):
    artifact = tmp_path / "artifact.json"
    artifact.write_text(json.dumps({"segments": [{"text": "ok", "start": 0, "end": 1}]}))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "dataset_name": "suite",
        "cases": [{"id": "one", "language": "en", "reference": "artifact.json",
                   "prediction": "artifact.json", "consent": True, "ownership": "user-owned"}],
        "thresholds": {"max_peak_memory_mb": 1000}}))

    result = CliRunner().invoke(app, ["benchmark-speech", str(manifest)])

    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_thresholds_apply_to_each_case_not_only_the_average(tmp_path):
    good = tmp_path / "good.json"
    bad = tmp_path / "bad.json"
    reference = tmp_path / "reference.json"
    reference.write_text(json.dumps({"segments": [{"text": "one two", "start": 0, "end": 1}]}))
    good.write_text(reference.read_text())
    bad.write_text(json.dumps({"segments": [{"text": "wrong words", "start": 0, "end": 1}]}))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "dataset_name": "suite", "cases": [
        {"id": case_id, "language": "en", "reference": "reference.json",
         "prediction": prediction, "consent": True, "ownership": "user-owned"}
        for case_id, prediction in (("good", "good.json"), ("bad", "bad.json"))
    ], "thresholds": {"max_wer": 0.6}}))

    report = run_manifest(manifest)

    assert report["passed"] is False
    assert report["failures"] == [
        {"case_id": "bad", "metric": "wer", "value": 1.0, "maximum": 0.6}
    ]
