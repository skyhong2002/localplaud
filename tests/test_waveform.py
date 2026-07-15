"""Local waveform extraction, cache, API, and player UI."""

from __future__ import annotations

import subprocess
import time

import numpy as np


def test_waveform_extracts_normalized_peaks_and_uses_cache(monkeypatch, tmp_path):
    from localplaud.waveform import waveform_peaks

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    calls = []
    samples = np.array([0, 100, -200, 400, -800, 1600, -3200, 6400], dtype="<i2")

    def fake_run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args[0], 0, stdout=samples.tobytes(), stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    first = waveform_peaks(audio, buckets=32)
    second = waveform_peaks(audio, buckets=32)
    assert first == second and len(first) == 32
    assert max(first) == 1.0 and min(first) >= 0
    assert len(calls) == 1
    assert (tmp_path / "waveform-32.json").exists()


def test_waveform_api_and_custom_player(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'wave.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.models import FileStatus, PlaudFile, Transcript
    from localplaud.db.session import init_db, session_scope

    init_db()
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="wave",
                filename="Waveform",
                status=FileStatus.downloaded,
                audio_path=str(audio),
            )
        )
        session.add(
            Transcript(
                file_id="wave",
                provider="seed",
                source="local",
                text="hello",
                segments=[{"text": "hello", "start": 0.0, "end": 1.0}],
            )
        )
    def fake_waveform(path, buckets):
        time.sleep(0.02)
        return [0.2] * buckets

    monkeypatch.setattr("localplaud.waveform.waveform_peaks", fake_waveform)
    client = TestClient(app)
    response = client.get("/audio/wave/waveform?buckets=40")
    assert response.status_code == 202
    for _ in range(50):
        response = client.get("/audio/wave/waveform?buckets=40")
        if response.status_code == 200:
            break
        time.sleep(0.01)
    assert response.status_code == 200
    assert response.json()["buckets"] == 40 and len(response.json()["peaks"]) == 40
    page = client.get("/file/wave")
    assert 'id="persistent-player"' in page.text
    for control in ("player-toggle", "player-back", "player-forward", "waveform", "player-speed"):
        assert f'id="{control}"' in page.text
    assert client.get("/audio/missing/waveform").status_code == 409
