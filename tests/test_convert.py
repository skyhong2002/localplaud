"""Audio conversion: ffmpeg detection and failure handling (no real audio needed)."""

from __future__ import annotations

import shutil
import subprocess
import wave

import pytest

import localplaud.worker.convert as convert
from localplaud.worker.convert import ConversionError, ffmpeg_available, to_wav

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


def test_ffmpeg_available_returns_bool():
    assert isinstance(ffmpeg_available(), bool)
    assert ffmpeg_available() is _HAS_FFMPEG


def test_to_wav_raises_when_ffmpeg_missing(monkeypatch, tmp_path):
    """Force the no-ffmpeg path regardless of the host machine."""
    monkeypatch.setattr(convert.shutil, "which", lambda name: None)

    assert ffmpeg_available() is False
    with pytest.raises(ConversionError, match="ffmpeg not found"):
        to_wav(tmp_path / "in.opus", tmp_path / "out.wav")
    # Failed before touching the filesystem.
    assert not (tmp_path / "out.wav").exists()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_to_wav_raises_on_nonexistent_input(tmp_path):
    src = tmp_path / "does-not-exist.opus"
    dst = tmp_path / "nested" / "out.wav"

    with pytest.raises(ConversionError, match="ffmpeg failed"):
        to_wav(src, dst)

    # The destination's parent dir is prepared even though conversion failed.
    assert dst.parent.is_dir()
    assert not dst.exists()


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_to_wav_raises_on_garbage_input(tmp_path):
    """An existing but non-audio file must surface a ConversionError, not a
    zero-exit success."""
    src = tmp_path / "garbage.opus"
    src.write_bytes(b"this is not audio")

    with pytest.raises(ConversionError, match="ffmpeg failed"):
        to_wav(src, tmp_path / "out.wav")


def _ogg_packets(data: bytes) -> list[bytes]:
    """Extract complete packets from ffmpeg's synthetic Ogg Opus output."""
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


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_recovers_validated_fixed_packet_opus(tmp_path):
    encoded = tmp_path / "synthetic.ogg"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=2",
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
            str(encoded),
        ],
        check=True,
    )
    packets = _ogg_packets(encoded.read_bytes())[2:]
    assert len(packets) >= 50
    assert all(len(packet) == 80 and packet[0] == 0xB8 for packet in packets)
    src = tmp_path / "synthetic.opus"
    original = b"".join(packets)
    src.write_bytes(original)
    dst = tmp_path / "out.wav"
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src), "-f", "null", "-"],
        capture_output=True,
    )
    assert probe.returncode != 0  # This test must exercise the recovery path.

    assert to_wav(src, dst, expected_duration_seconds=len(packets) * 0.02) == dst
    assert src.read_bytes() == original
    with wave.open(str(dst)) as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        assert wav.getsampwidth() == 2
        assert abs(wav.getnframes() / wav.getframerate() - len(packets) * 0.02) < 0.03
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "out.wav",
        "synthetic.ogg",
        "synthetic.opus",
    ]


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_regular_media_uses_ffmpeg_and_replaces_destination_atomically(tmp_path):
    src = tmp_path / "tone.wav"
    with wave.open(str(src), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 8000)
    original = src.read_bytes()
    dst = tmp_path / "out.wav"
    dst.write_bytes(b"previous output")
    to_wav(src, dst)
    assert src.read_bytes() == original
    with wave.open(str(dst)) as wav:
        assert wav.getframerate() == 16000
        assert wav.getnchannels() == 1
        assert wav.getnframes() == 16000


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
@pytest.mark.parametrize(
    "name,raw",
    [
        ("wrong.bin", (bytes([0xB8]) + bytes(79)) * 50),
        ("short.opus", (bytes([0xB8]) + bytes(79)) * 49),
        ("truncated.opus", (bytes([0xB8]) + bytes(79)) * 50 + b"x"),
        ("wrong-toc.opus", (bytes([0xB8]) + bytes(79)) * 49 + bytes(80)),
    ],
)
def test_rejects_unverified_raw_layout_without_touching_destination(tmp_path, name, raw):
    src = tmp_path / name
    src.write_bytes(raw)
    dst = tmp_path / "out.wav"
    dst.write_bytes(b"existing output")
    with pytest.raises(ConversionError):
        to_wav(src, dst)
    assert src.read_bytes() == raw
    assert dst.read_bytes() == b"existing output"
    assert {path.name for path in tmp_path.iterdir()} == {name, "out.wav"}


@pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed")
def test_raw_duration_mismatch_preserves_existing_output(tmp_path):
    src = tmp_path / "fixed.opus"
    src.write_bytes((bytes([0xB8]) + bytes(79)) * 50)
    dst = tmp_path / "out.wav"
    dst.write_bytes(b"existing output")
    with pytest.raises(ConversionError, match="duration differs"):
        to_wav(src, dst, expected_duration_seconds=10)
    assert dst.read_bytes() == b"existing output"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["fixed.opus", "out.wav"]


def test_fallback_decode_failure_cleans_up_and_preserves_destination(monkeypatch, tmp_path):
    src = tmp_path / "fixed.opus"
    original = (bytes([0xB8]) + bytes(79)) * 50
    src.write_bytes(original)
    dst = tmp_path / "out.wav"
    dst.write_bytes(b"previous output")
    calls = []

    def failing_decode(input_path, output_path, sample_rate, *, strict=False):
        calls.append(strict)
        output_path.write_bytes(b"partial output")
        raise ConversionError("decoder failed")

    monkeypatch.setattr(convert, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(convert, "_decode", failing_decode)
    with pytest.raises(ConversionError, match="decoder failed"):
        to_wav(src, dst)
    assert calls == [False, True]
    assert src.read_bytes() == original
    assert dst.read_bytes() == b"previous output"
    assert {path.name for path in tmp_path.iterdir()} == {"fixed.opus", "out.wav"}
