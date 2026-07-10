"""ASR via whisper.cpp's CLI — local, uses Metal on Apple Silicon.

Shells out to ``whisper-cli`` (configurable) with JSON output and parses the
result. No word timestamps or speaker labels; diarization fills speakers in.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import AsrError, AsrUnavailable, Segment, Transcript
from .registry import register

log = logging.getLogger(__name__)


class WhisperCppProvider:
    name = "whispercpp"

    def __init__(self, cfg):
        self.cfg = cfg.whispercpp
        self.language = cfg.language

    def available(self) -> bool:
        return shutil.which(self.cfg.binary) is not None and self.cfg.model_path.exists()

    def transcribe(self, audio_path: Path, language: str = "auto") -> Transcript:
        binary = shutil.which(self.cfg.binary)
        if binary is None:
            raise AsrUnavailable(f"whisper.cpp binary {self.cfg.binary!r} not found on PATH")
        if not self.cfg.model_path.exists():
            raise AsrUnavailable(f"whisper.cpp model not found: {self.cfg.model_path}")

        with tempfile.TemporaryDirectory(prefix="localplaud-whispercpp-") as tmpdir:
            out_base = Path(tmpdir) / "transcript"
            cmd = [
                binary,
                "-m", str(self.cfg.model_path),
                "-f", str(audio_path),
                "-oj",
                "-of", str(out_base),
                *self.cfg.extra_args,
            ]
            if language != "auto":
                cmd += ["-l", language]
            log.info("Running whisper.cpp: %s", " ".join(cmd))
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise AsrError(
                    f"whisper.cpp exited with {result.returncode}: {result.stderr.strip()}"
                )

            json_path = out_base.with_suffix(".json")
            if not json_path.exists():
                raise AsrError(f"whisper.cpp produced no JSON output at {json_path}")
            with json_path.open("rb") as fh:
                data = json.load(fh)

        segments = []
        for entry in data.get("transcription", []):
            offsets = entry.get("offsets", {})
            segments.append(
                Segment(
                    text=entry.get("text", ""),
                    start=offsets.get("from", 0) / 1000,
                    end=offsets.get("to", 0) / 1000,
                )
            )

        detected = data.get("result", {}).get("language")
        return Transcript(
            segments=segments,
            language=detected or (None if language == "auto" else language),
            duration=segments[-1].end if segments else None,
            provider=self.name,
            model=str(self.cfg.model_path),
            has_speakers=False,
        )


@register("whispercpp")
def _factory(cfg):
    return WhisperCppProvider(cfg)
