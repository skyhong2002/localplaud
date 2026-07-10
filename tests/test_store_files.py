"""Filesystem layout for downloaded audio: id-addressed dirs under download_dir."""

from __future__ import annotations

import os

from localplaud.config import get_settings
from localplaud.store.files import audio_path, file_dir, wav_path


def _use_tmp_download_dir(monkeypatch, tmp_path):
    """Point poller.download_dir at a tmp dir and reload the settings singleton."""
    for key in list(os.environ):
        if key.startswith("LOCALPLAUD_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)  # no config.toml or .env here
    download_dir = tmp_path / "audio"
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(download_dir))
    get_settings(reload=True)
    return download_dir


def test_file_dir_builds_under_download_dir_and_creates_it(monkeypatch, tmp_path):
    download_dir = _use_tmp_download_dir(monkeypatch, tmp_path)
    assert not download_dir.exists()  # nothing created just by configuring

    d = file_dir("dab5c6ca728964152f32d93ed76c1950")

    assert d == download_dir / "dab5c6ca728964152f32d93ed76c1950"
    assert d.is_dir()  # created (parents included) as a side effect


def test_file_dir_is_idempotent(monkeypatch, tmp_path):
    _use_tmp_download_dir(monkeypatch, tmp_path)
    assert file_dir("abc") == file_dir("abc")  # second call must not raise


def test_audio_path_default_and_custom_ext(monkeypatch, tmp_path):
    download_dir = _use_tmp_download_dir(monkeypatch, tmp_path)

    p = audio_path("abc")
    assert p == download_dir / "abc" / "audio.opus"

    mp3 = audio_path("abc", ext="mp3")
    assert mp3 == download_dir / "abc" / "audio.mp3"

    # Parent dir exists so the caller can write straight to the path.
    assert p.parent.is_dir()


def test_wav_path_sibling_of_original(monkeypatch, tmp_path):
    download_dir = _use_tmp_download_dir(monkeypatch, tmp_path)

    w = wav_path("abc")

    assert w == download_dir / "abc" / "audio.wav"
    assert w.parent == audio_path("abc").parent
