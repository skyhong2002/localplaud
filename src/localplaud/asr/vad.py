"""Provider-agnostic Voice Activity Detection (VAD) for the ASR pipeline.

VAD is the first stage of the speech pipeline
(``VAD -> Whisper large-v3-turbo ASR -> word-level alignment -> diarization``).
Trimming non-speech before ASR reduces hallucination on long silences and lets a
long recording be transcribed in bounded speech chunks whose timestamps stay
globally correct.

This module is optional and degrades honestly. It uses the ``silero-vad``
package (torch backend) when it is importable, and otherwise raises
:class:`VadUnavailable` with an actionable message naming the missing package and
the ``vad`` pyproject extra. Callers (the ASR providers) must catch that, log a
warning, fall back to whole-file transcription, and surface the degraded state in
their ``health()`` — never pretend VAD ran.

The pure geometry helper :func:`merge_speech_regions` has no optional
dependency and is always available for chunk planning and testing.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# silero-vad and the wav we feed it are 16 kHz mono (see worker/convert.py).
SAMPLE_RATE = 16_000


class VadError(RuntimeError):
    """A VAD stage failed at runtime (e.g. ffmpeg could not slice a region)."""


class VadUnavailable(VadError):
    """VAD cannot run here (the optional ``silero-vad`` dependency is missing).

    Callers should catch this and fall back to whole-file transcription rather
    than treating it as a hard failure.
    """


def detect_speech(wav_path, cfg) -> list[tuple[float, float]]:
    """Return speech regions as ``(start_s, end_s)`` tuples using silero-vad.

    ``wav_path`` should be 16 kHz mono PCM (the pipeline's canonical ASR wav).
    Raises :class:`VadUnavailable` when the optional dependency is not installed.
    """
    try:
        import torch  # noqa: F401  (silero-vad uses the torch backend)
        from silero_vad import get_speech_timestamps, load_silero_vad, read_audio
    except ImportError as exc:
        raise VadUnavailable(
            "silero-vad is not installed; install the optional 'vad' extra "
            "(pip install 'localplaud[vad]') to enable VAD pre-segmentation"
        ) from exc

    model = load_silero_vad()
    audio = read_audio(str(wav_path), sampling_rate=SAMPLE_RATE)
    timestamps = get_speech_timestamps(
        audio,
        model,
        sampling_rate=SAMPLE_RATE,
        threshold=cfg.threshold,
        min_speech_duration_ms=cfg.min_speech_ms,
        min_silence_duration_ms=cfg.min_silence_ms,
        speech_pad_ms=cfg.speech_pad_ms,
        return_seconds=True,
    )
    return [(float(t["start"]), float(t["end"])) for t in timestamps]


def merge_speech_regions(
    regions: list[tuple[float, float]],
    min_gap_s: float,
    pad_s: float,
    max_region_s: float,
) -> list[tuple[float, float]]:
    """Normalise raw speech regions into ASR-ready chunks.

    Steps, in order:

    1. Drop empty/reversed spans and sort by start.
    2. Merge neighbours separated by a gap of ``<= min_gap_s`` (this also
       collapses overlaps).
    3. Pad each region by ``pad_s`` on both sides (clamped at 0), then re-merge
       any regions the padding pushed into overlap.
    4. Split regions longer than ``max_region_s`` into consecutive windows so no
       single ASR call exceeds the chunk budget. A falsy ``max_region_s``
       disables splitting.

    Returns a list of ``(start_s, end_s)`` tuples. Empty input yields ``[]``.
    """
    ordered = sorted((float(s), float(e)) for s, e in regions if float(e) > float(s))
    if not ordered:
        return []

    merged: list[list[float]] = [list(ordered[0])]
    for start, end in ordered[1:]:
        last = merged[-1]
        if start - last[1] <= min_gap_s:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])

    padded = [(max(0.0, start - pad_s), end + pad_s) for start, end in merged]

    remerged: list[list[float]] = [list(padded[0])]
    for start, end in padded[1:]:
        last = remerged[-1]
        if start <= last[1]:
            last[1] = max(last[1], end)
        else:
            remerged.append([start, end])

    result: list[tuple[float, float]] = []
    for start, end in remerged:
        if max_region_s and (end - start) > max_region_s:
            cursor = start
            while cursor < end:
                result.append((cursor, min(cursor + max_region_s, end)))
                cursor += max_region_s
        else:
            result.append((start, end))
    return result


def slice_region(src, start_s: float, end_s: float, dst) -> Path:
    """Extract ``[start_s, end_s)`` of ``src`` into ``dst`` as 16 kHz mono wav.

    Uses the same ffmpeg pattern as worker/convert.py so chunked ASR sees the
    canonical PCM format. Raises :class:`VadUnavailable` if ffmpeg is missing so
    the caller can fall back, and :class:`VadError` if the slice itself fails.
    """
    if shutil.which("ffmpeg") is None:
        raise VadUnavailable("ffmpeg not found on PATH — required to slice VAD speech regions")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.0, end_s - start_s)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start_s:.3f}",
        "-i",
        str(src),
        "-t",
        f"{duration:.3f}",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VadError(f"ffmpeg failed slicing region ({proc.returncode}): {proc.stderr[-500:]}")
    return dst


def health(cfg) -> tuple[bool, str]:
    """Report whether VAD is enabled and able to run, mirroring worker.diarize.health.

    Returns ``(ok, detail)``. ``ok`` is False when VAD is disabled or the
    optional dependency is missing; the detail is a human-readable, actionable
    string suitable for a health/status surface.
    """
    if not cfg.enabled:
        return False, "disabled; ASR runs on the whole file without VAD pre-segmentation"
    try:
        import silero_vad  # noqa: F401
    except ImportError as exc:
        return False, (
            f"enabled but silero-vad is not installed ({exc}); install the optional "
            "'vad' extra (pip install 'localplaud[vad]'). ASR falls back to whole-file "
            "transcription"
        )
    try:
        import torch  # noqa: F401
    except ImportError as exc:
        return False, (
            f"enabled but torch is not installed ({exc}); the 'vad' extra pulls it in. "
            "ASR falls back to whole-file transcription"
        )
    return True, "enabled; silero-vad available"
