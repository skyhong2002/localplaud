"""Per-recording Custom speech settings (ASR language and speaker count)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

import localplaud.config as config
import localplaud.db.session as db_session
from localplaud.api.app import app
from localplaud.asr.base import Segment, Transcript
from localplaud.config import DiarizeConfig, Settings
from localplaud.db.migrations import migrate_speech_override_column
from localplaud.db.models import Base, PlaudFile, TranscriptRevision
from localplaud.db.models import Transcript as TranscriptRow
from localplaud.providers.service import (
    bootstrap_default_profile,
    normalize_speech_overrides,
    resolve_recording_profile,
    set_recording_speech_overrides,
)
from localplaud.remote import server as remote_server
from localplaud.remote.protocol import JobStage, JobSubmitRequest
from localplaud.worker import diarize as diarize_module
from localplaud.worker import pipeline
from localplaud.worker.pipeline import _settings_for_stage, _speech_override_detail


def _session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'speech.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    bootstrap_default_profile(session, Settings())
    session.add(PlaudFile(id="rec", filename="Weekly sync"))
    session.flush()
    return session


def test_normalize_rejects_conflicts_and_bad_values():
    assert normalize_speech_overrides({}) == {}
    assert normalize_speech_overrides({"language": "", "num_speakers": ""}) == {}
    assert normalize_speech_overrides({"language": "zh", "num_speakers": "3"}) == {
        "language": "zh",
        "num_speakers": 3,
    }
    assert normalize_speech_overrides({"min_speakers": 2, "max_speakers": 4}) == {
        "min_speakers": 2,
        "max_speakers": 4,
    }
    for bad in (
        {"language": "klingon"},
        {"num_speakers": 0},
        {"num_speakers": 2, "min_speakers": 1},
        {"min_speakers": 5, "max_speakers": 2},
        {"vad": {}},
    ):
        with pytest.raises(ValueError):
            normalize_speech_overrides(bad)


def test_override_is_durable_resolved_with_provenance_and_projected(tmp_path):
    session = _session(tmp_path)
    baseline = resolve_recording_profile(session, "rec").to_dict()
    assert "language" not in baseline["stages"]["transcribe"].get("options", {})

    set_recording_speech_overrides(
        session, "rec", {"language": "zh", "min_speakers": 2, "max_speakers": 4}
    )
    session.commit()
    session.expire_all()
    assert session.get(PlaudFile, "rec").speech_overrides == {
        "language": "zh",
        "min_speakers": 2,
        "max_speakers": 4,
    }

    resolved = resolve_recording_profile(session, "rec").to_dict()
    assert resolved["layers"][-1] == "recording-speech:rec"
    assert resolved["layer_provenance"][-1] == {
        "kind": "recording_speech",
        "file_id": "rec",
        "speech_overrides": {"language": "zh", "min_speakers": 2, "max_speakers": 4},
    }
    # The patch keeps the profile's provider/model selection.
    assert resolved["stages"]["transcribe"]["connection"] == baseline["stages"]["transcribe"][
        "connection"
    ]
    assert resolved["stages"]["transcribe"]["options"]["language"] == "zh"
    assert resolved["stages"]["diarize"]["options"] == {
        "max_speakers": 4,
        "min_speakers": 2,
        "num_speakers": None,
    }

    settings = Settings(diarize={"num_speakers": 5})
    asr = _settings_for_stage(settings, resolved, "transcribe")
    diar = _settings_for_stage(settings, resolved, "diarize")
    assert asr.asr.language == "zh"
    assert (diar.diarize.num_speakers, diar.diarize.min_speakers, diar.diarize.max_speakers) == (
        None,
        2,
        4,
    )
    assert settings.diarize.num_speakers == 5  # never mutates host defaults
    assert _speech_override_detail(resolved, "transcribe", None) == {
        "speech_overrides": {"language": "zh"}
    }

    set_recording_speech_overrides(session, "rec", {})
    assert session.get(PlaudFile, "rec").speech_overrides is None
    assert resolve_recording_profile(session, "rec").to_dict()["layers"][-1] != "recording-speech:rec"


def test_saving_override_keeps_transcript_edits_and_stage_runs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    config.get_settings(reload=True)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    with TestClient(app) as client:
        audio = tmp_path / "rec.opus"
        audio.write_bytes(b"OggS")
        with db_session.session_scope() as session:
            session.add(PlaudFile(id="rec", filename="Weekly sync", audio_path=str(audio)))
            session.flush()
            row = TranscriptRow(
                file_id="rec",
                provider="faster-whisper",
                source="local",
                segments=[{"start": 0.0, "end": 1.0, "text": "原始"}],
                text="原始",
            )
            session.add(row)
            session.flush()
            session.add(
                TranscriptRevision(
                    file_id="rec",
                    base_transcript_id=row.id,
                    segments=[{"start": 0.0, "end": 1.0, "text": "使用者修正"}],
                    text="使用者修正",
                )
            )
        response = client.put(
            "/api/files/rec/speech-settings", json={"language": "zh", "num_speakers": 3}
        )
        assert response.status_code == 200, response.text
        assert response.json()["speech_overrides"] == {"language": "zh", "num_speakers": 3}
        bad = client.put("/api/files/rec/speech-settings", json={"language": "xx"})
        assert bad.status_code == 422
        assert client.put("/api/files/missing/speech-settings", json={}).status_code == 404
        with db_session.session_scope() as session:
            assert session.get(PlaudFile, "rec").speech_overrides == {
                "language": "zh",
                "num_speakers": 3,
            }
            assert session.query(TranscriptRow).filter_by(file_id="rec").count() == 1
            revision = session.query(TranscriptRevision).filter_by(file_id="rec").one()
            assert revision.text == "使用者修正"
            assert revision.base_transcript_id is not None
        page = client.get("/file/rec")
        assert page.status_code == 200
        assert 'id="speech-settings-dialog"' in page.text
        assert '<option value="zh" selected>' in page.text
        assert 'name="num_speakers" min="1" max="20" inputmode="numeric" value="3"' in page.text


def test_diarize_passes_exact_or_range_to_pyannote(monkeypatch):
    calls = []

    class FakePipeline:
        def __call__(self, path, **kwargs):
            calls.append(kwargs)
            return []

    monkeypatch.setattr(diarize_module, "_load_pipeline", lambda cfg: FakePipeline())
    transcript = Transcript(segments=[Segment(start=0, end=1, text="hi")])
    diarize_module.diarize("a.wav", transcript, DiarizeConfig(num_speakers=3, min_speakers=1))
    diarize_module.diarize("a.wav", transcript, DiarizeConfig(min_speakers=2, max_speakers=4))
    assert calls == [{"num_speakers": 3}, {"min_speakers": 2, "max_speakers": 4}]


def test_remote_worker_applies_and_acknowledges_overrides(monkeypatch, tmp_path):
    seen = {}

    def fake_run_asr(path, settings):
        seen["language"] = settings.asr.language
        return Transcript(segments=[Segment(start=0, end=1, text="你好 hello")], language="zh")

    def fake_diarize(path, transcript, cfg):
        seen["speakers"] = (cfg.num_speakers, cfg.min_speakers, cfg.max_speakers)
        return transcript

    monkeypatch.setattr("localplaud.worker.transcribe.run_asr", fake_run_asr)
    monkeypatch.setattr("localplaud.worker.diarize.diarize", fake_diarize)
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    audio = pipeline._remote_audio_input(wav)
    artifacts = remote_server._execute(
        JobSubmitRequest(
            idempotency_key="k" * 64,
            stage=JobStage.transcribe,
            inputs=[audio],
            options={"language": "zh"},
        )
    )
    payload = json.loads(remote_server.base64.b64decode(artifacts[0]["data_base64"]))
    assert seen["language"] == "zh"
    assert payload["applied_speech_overrides"] == {"language": "zh"}

    transcript_input = pipeline._remote_json_input(
        "transcript", {"segments": [{"start": 0, "end": 1, "text": "hi"}]}
    )
    artifacts = remote_server._execute(
        JobSubmitRequest(
            idempotency_key="d" * 64,
            stage=JobStage.diarize,
            inputs=[audio, transcript_input],
            options={"num_speakers": None, "min_speakers": 2, "max_speakers": 4},
        )
    )
    payload = json.loads(remote_server.base64.b64decode(artifacts[0]["data_base64"]))
    assert seen["speakers"] == (None, 2, 4)
    assert payload["applied_speech_overrides"]["min_speakers"] == 2


def test_older_remote_worker_without_ack_is_marked_unconfirmed():
    snapshot = {
        "layer_provenance": [{"kind": "recording_speech"}],
        "stages": {"diarize": {"options": {"num_speakers": 3}}},
    }
    assert pipeline._remote_speech_ack(snapshot, "diarize", {"segments": []}) is False
    assert _speech_override_detail(snapshot, "diarize", False) == {
        "speech_overrides": {"num_speakers": 3},
        "speech_overrides_unconfirmed": True,
    }
    assert pipeline._remote_speech_ack({"stages": {}}, "diarize", {}) is None


def test_speech_override_migration_is_additive_and_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO plaud_files (id) VALUES ('old')"))
    assert migrate_speech_override_column(engine) == ["plaud_files.speech_overrides"]
    assert migrate_speech_override_column(engine) == []
    columns = {column["name"] for column in inspect(engine).get_columns("plaud_files")}
    assert "speech_overrides" in columns
    assert Path(tmp_path / "legacy.db").exists()
