"""Audio conversion — opus -> 16kHz mono wav via ffmpeg.

ASR engines want 16kHz mono PCM. We keep the original .opus untouched and
write a sibling .wav for processing.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


class ConversionError(RuntimeError):
    pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def to_wav(src: Path, dst: Path, sample_rate: int = 16000) -> Path:
    """Convert ``src`` to 16kHz mono wav at ``dst``. Returns ``dst``."""
    if not ffmpeg_available():
        raise ConversionError("ffmpeg not found on PATH — required for audio conversion")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    log.debug("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ConversionError(f"ffmpeg failed ({proc.returncode}): {proc.stderr[-800:]}")
    return dst
