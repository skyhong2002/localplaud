"""Audio conversion: ffmpeg detection and failure handling (no real audio needed)."""

from __future__ import annotations

import shutil

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
