from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner


def _setup(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'bench.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)

    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageAttempt,
        StageName,
        StageStatus,
        Transcript,
    )
    from localplaud.db.session import init_db, session_scope

    init_db()
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="bench",
                filename="Private benchmark",
                duration_ms=4000,
                status=FileStatus.done,
                transcripts=[
                    Transcript(
                        provider="fake",
                        model="turbo",
                        source="local",
                        text="你好 世界 Next steps",
                        segments=[
                            {
                                "start": 0.1,
                                "end": 2.1,
                                "speaker": "HYP_A",
                                "text": "你好 世界",
                            },
                            {
                                "start": 2.1,
                                "end": 4.1,
                                "speaker": "HYP_B",
                                "text": "Next steps",
                            },
                        ],
                    )
                ],
            )
        )
        session.add(
            StageAttempt(
                file_id="bench",
                stage=StageName.transcribe,
                attempt=1,
                status=StageStatus.completed,
                provider="fake",
                model="turbo",
                latency_ms=2000,
                usage={"process_peak_memory_mb": 321.25},
            )
        )


def _reference():
    return {
        "schema": "localplaud-benchmark-reference/v1",
        "language": "zh-TW+en",
        "case": "code-switch",
        "coverage": "full_audio",
        "segments": [
            {"start": 0.0, "end": 2.0, "speaker": "REF_1", "text": "你好 世界"},
            {"start": 2.0, "end": 4.0, "speaker": "REF_2", "text": "Next step"},
        ],
    }


def test_speaker_metrics_count_overlapping_speaker_time():
    from localplaud.benchmark import _speaker_metrics

    reference = [
        {"start": 0, "end": 4, "speaker": "REF_A"},
        {"start": 1, "end": 3, "speaker": "REF_B"},
    ]
    perfect = [
        {"start": 0, "end": 4, "speaker": "HYP_X"},
        {"start": 1, "end": 3, "speaker": "HYP_Y"},
    ]
    missing_overlap = [{"start": 0, "end": 4, "speaker": "HYP_X"}]

    assert _speaker_metrics(reference, perfect)["der"] == 0
    metrics = _speaker_metrics(reference, missing_overlap)
    assert metrics["der"] == pytest.approx(2 / 6)
    assert metrics["miss_seconds"] == 2
    assert metrics["reference_speech_seconds"] == 6
    assert metrics["overlap_aware"] is True


def test_edit_breakdown_reports_reference_aligned_insertions_without_tokens():
    from localplaud.benchmark import _edit_breakdown

    metrics = _edit_breakdown(["a", "b", "c"], ["a", "x", "b", "d"])
    assert metrics == {
        "substitutions": 1,
        "deletions": 0,
        "insertions": 1,
        "errors": 2,
        "reference_units": 3,
        "hypothesis_units": 4,
        "error_rate": pytest.approx(2 / 3),
        "insertion_rate": pytest.approx(1 / 3),
    }
    assert not any(isinstance(value, list) for value in metrics.values())


def test_benchmark_reports_accuracy_speakers_timestamps_and_rtf(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from localplaud.benchmark import benchmark_recording

    report = benchmark_recording("bench", _reference())
    assert report["schema"] == "localplaud-benchmark-report/v1"
    assert report["accuracy"]["wer"] == pytest.approx(0.25)
    assert 0 < report["accuracy"]["cer"] < 0.2
    assert report["speakers"]["der"] == pytest.approx(0.075)
    assert report["speakers"]["speaker_mapping"] == {"HYP_A": "REF_1", "HYP_B": "REF_2"}
    assert report["timestamps"] == {"boundary_mae_seconds": 0.1, "paired_segments": 2}
    assert report["hallucination"]["non_speech_character_rate"] == pytest.approx(0.034615)
    assert report["hallucination"]["majority_non_speech_segments"] == 0
    assert report["hallucination"]["speech_character_insertions"] == 1
    assert report["hallucination"]["speech_word_insertions"] == 0
    assert report["accuracy"]["word_errors"]["substitutions"] == 1
    assert report["accuracy"]["word_errors"]["deletions"] == 0
    assert report["execution"]["real_time_factor"] == 0.5
    assert report["execution"]["peak_memory_mb"] == 321.25
    assert report["execution"]["memory_scope"] == "worker_process_high_water_rss"
    assert "text" not in report["reference"]


def test_benchmark_counts_hypothesis_text_outside_annotated_speech(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from localplaud.benchmark import benchmark_recording
    from localplaud.db.models import Transcript
    from localplaud.db.session import session_scope

    with session_scope() as session:
        transcript = session.query(Transcript).filter_by(file_id="bench", source="local").one()
        transcript.segments = list(transcript.segments) + [
            {
                "start": 4.5,
                "end": 5.5,
                "speaker": "HYP_A",
                "text": "hallucinated tail",
            }
        ]
    metric = benchmark_recording("bench", _reference())["hallucination"]
    assert metric["non_speech_character_rate"] > 0
    assert metric["estimated_non_speech_characters"] > 0
    assert metric["majority_non_speech_segments"] == 1

    incomplete = _reference() | {"coverage": "partial"}
    unavailable = benchmark_recording("bench", incomplete)["hallucination"]
    assert unavailable["non_speech_character_rate"] is None
    assert unavailable["reason"] == "reference coverage is not full_audio"


def test_benchmark_cli_json_and_reference_validation(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from localplaud.benchmark import load_reference
    from localplaud.cli import app

    reference_path = tmp_path / "private-reference.json"
    reference_path.write_text(json.dumps(_reference()), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["benchmark-recording", "bench", "--reference", str(reference_path), "--json"],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["file_id"] == "bench"

    reference_path.write_text(json.dumps({"schema": "wrong", "segments": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="reference schema"):
        load_reference(reference_path)


def test_benchmark_upload_api_is_bounded_and_does_not_persist_reference(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient

    from localplaud.api.app import app

    private_content = json.dumps(_reference()).encode()
    with TestClient(app) as client:
        response = client.post(
            "/api/files/bench/benchmark",
            files={"reference": ("consented-private.json", private_content, "application/json")},
        )
        invalid = client.post(
            "/api/files/bench/benchmark",
            files={"reference": ("invalid.json", b"not-json", "application/json")},
        )
    assert response.status_code == 200
    assert response.json()["schema"] == "localplaud-benchmark-report/v1"
    assert invalid.status_code == 422
    assert not list(tmp_path.rglob("consented-private.json"))
