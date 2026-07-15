"""Small cached waveform envelopes generated locally with ffmpeg."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np


def cached_waveform_peaks(audio_path: str | Path, *, buckets: int = 180) -> list[float] | None:
    path = Path(audio_path)
    if not path.exists():
        raise FileNotFoundError(path)
    buckets = min(max(int(buckets), 32), 500)
    cache = path.parent / f"waveform-{buckets}.json"
    signature = {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        if payload.get("signature") == signature and len(payload.get("peaks", [])) == buckets:
            return payload["peaks"]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def waveform_peaks(audio_path: str | Path, *, buckets: int = 180) -> list[float]:
    path = Path(audio_path)
    cached = cached_waveform_peaks(path, buckets=buckets)
    if cached is not None:
        return cached
    buckets = min(max(int(buckets), 32), 500)
    cache = path.parent / f"waveform-{buckets}.json"
    signature = {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "100",
            "-f", "s16le", "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=180,
    )
    samples = np.abs(np.frombuffer(result.stdout, dtype="<i2").astype(np.float32))
    if not samples.size:
        peaks = [0.0] * buckets
    else:
        edges = np.linspace(0, samples.size, buckets + 1, dtype=int)
        raw = []
        for index in range(buckets):
            start = min(edges[index], samples.size - 1)
            end = min(samples.size, max(edges[index + 1], start + 1))
            raw.append(float(samples[start:end].max()))
        scale = max(np.percentile(raw, 95), 1.0)
        peaks = [round(min(1.0, value / scale), 4) for value in raw]
    try:
        cache.write_text(json.dumps({"signature": signature, "peaks": peaks}), encoding="utf-8")
    except OSError:
        pass
    return peaks
