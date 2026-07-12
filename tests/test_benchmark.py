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
            )
        )


def _reference():
    return {
        "schema": "localplaud-benchmark-reference/v1",
        "language": "zh-TW+en",
        "case": "code-switch",
        "segments": [
            {"start": 0.0, "end": 2.0, "speaker": "REF_1", "text": "你好 世界"},
            {"start": 2.0, "end": 4.0, "speaker": "REF_2", "text": "Next step"},
        ],
    }


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
    assert report["execution"]["real_time_factor"] == 0.5
    assert report["execution"]["peak_memory_mb"] is None
    assert "text" not in report["reference"]


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
