"""Filesystem layout for downloaded audio and derived media.

Audio bytes never go in the database. They live under the configured
``download_dir`` in a flat, id-addressed layout so a file is easy to find and
re-derive:

    <download_dir>/<file_id>/audio.opus     # original from the cloud
    <download_dir>/<file_id>/audio.wav      # 16kHz mono, for ASR
"""

from __future__ import annotations

from pathlib import Path

from ..config import get_settings


def file_dir(file_id: str) -> Path:
    d = Path(get_settings().poller.download_dir) / file_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def audio_path(file_id: str, ext: str = "opus") -> Path:
    return file_dir(file_id) / f"audio.{ext}"


def wav_path(file_id: str) -> Path:
    return file_dir(file_id) / "audio.wav"
