"""Headerless Plaud Opus is decoded for playback while export stays original."""

from __future__ import annotations

import shutil
import subprocess
import time
import wave

import pytest
from fastapi.testclient import TestClient


def _ogg_packets(data: bytes) -> list[bytes]:
    packets = []
    current = bytearray()
    offset = 0
    while offset < len(data):
        assert data[offset : offset + 4] == b"OggS"
        count = data[offset + 26]
        lacing = data[offset + 27 : offset + 27 + count]
        payload = offset + 27 + count
        for length in lacing:
            current.extend(data[payload : payload + length])
            payload += length
            if length < 255:
                packets.append(bytes(current))
                current.clear()
        offset = payload
    assert not current
    return packets


def _opus_audio(tmp_path, frequency: int) -> tuple[bytes, bytes]:
    ogg = tmp_path / f"tone-{frequency}.ogg"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=16000:duration=2",
            "-ac",
            "1",
            "-c:a",
            "libopus",
            "-application",
            "lowdelay",
            "-frame_duration",
            "20",
            "-b:a",
            "32k",
            "-vbr",
            "off",
            "-cutoff",
            "8000",
            "-f",
            "ogg",
            str(ogg),
        ],
        check=True,
    )
    packets = _ogg_packets(ogg.read_bytes())[2:]
    assert len(packets) >= 50
    assert all(len(packet) == 80 and packet[0] == 0xB8 for packet in packets)
    return b"".join(packets), ogg.read_bytes()


@pytest.fixture
def client_and_source(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.api.app import app
    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'playback.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()
    source = tmp_path / "recording.opus"
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="play",
                filename="Recording",
                status=FileStatus.done,
                audio_path=str(source),
                duration_ms=2020,
            )
        )
    return TestClient(app), source


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_headerless_playback_range_waveform_and_original_export(client_and_source, tmp_path):
    from localplaud.imports import ensure_playable_audio

    client, source = client_and_source
    raw, _ = _opus_audio(tmp_path, 440)
    source.write_bytes(raw)
    response = client.get("/audio/play", headers={"Range": "bytes=0-43"})
    assert response.status_code == 206
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["content-range"].startswith("bytes 0-43/")
    assert response.content[:4] == b"RIFF" and len(response.content) == 44
    playable = ensure_playable_audio("play")
    with wave.open(str(playable), "rb") as audio:
        assert audio.getnframes() > 0
    assert ensure_playable_audio("play") == playable
    for _ in range(80):
        waveform = client.get("/audio/play/waveform?buckets=40")
        if waveform.status_code == 200:
            break
        assert waveform.status_code == 202
        time.sleep(0.025)
    assert waveform.status_code == 200
    assert len(waveform.json()["peaks"]) == 40
    exported = client.get("/file/play/export/audio")
    assert exported.status_code == 200
    assert exported.content == raw == source.read_bytes()


def test_ordinary_and_ogg_opus_are_unchanged(client_and_source, tmp_path):
    from localplaud.imports import ensure_playable_audio

    client, source = client_and_source
    source.write_bytes(b"OggSunchanged")
    assert ensure_playable_audio("play") == source
    assert client.get("/audio/play").content == source.read_bytes()
    source.rename(source.with_suffix(".mp3"))
    mp3 = source.with_suffix(".mp3")
    mp3.write_bytes(b"ID3ordinary")
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.get(PlaudFile, "play").audio_path = str(mp3)
    assert ensure_playable_audio("play") == mp3
    assert client.get("/audio/play").content == b"ID3ordinary"


def test_malformed_headerless_opus_fails_clearly(client_and_source):
    client, source = client_and_source
    source.write_bytes((b"\xb8" + bytes(79)) * 49 + bytes(80))
    response = client.get("/audio/play")
    assert response.status_code == 502
    assert "unsupported headerless Opus packet layout" in response.json()["detail"]
    waveform = client.get("/audio/play/waveform")
    assert waveform.status_code == 502
    assert "unsupported headerless Opus packet layout" in waveform.json()["detail"]


def test_headerless_opus_requires_matching_duration_metadata(client_and_source):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    client, source = client_and_source
    source.write_bytes((b"\xb8" + bytes(79)) * 50)
    with session_scope() as session:
        session.get(PlaudFile, "play").duration_ms = None
    response = client.get("/audio/play")
    assert response.status_code == 502
    assert "requires recording duration metadata" in response.json()["detail"]
    with session_scope() as session:
        session.get(PlaudFile, "play").duration_ms = 5000
    response = client.get("/audio/play")
    assert response.status_code == 502
    assert "duration differs from recording metadata" in response.json()["detail"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_playback_cache_invalidates_when_original_changes(client_and_source, tmp_path):
    from localplaud.imports import ensure_playable_audio

    client, source = client_and_source
    first, _ = _opus_audio(tmp_path, 440)
    second, _ = _opus_audio(tmp_path, 660)
    source.write_bytes(first)
    first_path = ensure_playable_audio("play")
    first_wav = first_path.read_bytes()
    source.write_bytes(second)
    second_path = ensure_playable_audio("play")
    assert second_path != first_path
    assert second_path.read_bytes() != first_wav
    assert client.get("/file/play/export/audio").content == second
