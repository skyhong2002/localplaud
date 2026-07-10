"""Filesystem layout for downloaded audio and derived media.

Audio bytes never go in the database. They live under the configured
``download_dir`` in a flat, id-addressed layout so a file is easy to find and
re-derive:

    <download_dir>/<file_id>/audio.opus     # original from the cloud
    <download_dir>/<file_id>/audio.wav      # 16kHz mono, for ASR
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import get_settings

# Plaud file ids are hex-ish tokens. Validate before using an id in a path so a
# malicious/MITM'd cloud response can't traverse out of the download dir.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _safe_id(file_id: str) -> str:
    if not _ID_RE.match(file_id):
        raise ValueError(f"unsafe file id: {file_id!r}")
    return file_id


def file_dir(file_id: str) -> Path:
    d = Path(get_settings().poller.download_dir) / _safe_id(file_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def audio_path(file_id: str, ext: str = "opus") -> Path:
    return file_dir(file_id) / f"audio.{ext}"


def wav_path(file_id: str) -> Path:
    return file_dir(file_id) / "audio.wav"
