"""Qwen transcription and forced alignment in a disposable GPU process."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .base import AsrError, Segment, Transcript, Word
from .registry import register


def run_speech_process(kind: str, audio: Path, config: dict, python: str, timeout: int):
    with tempfile.TemporaryDirectory(prefix="localplaud-speech-") as directory:
        root = Path(directory)
        request, output = root / "request.json", root / "result.json"
        request.write_text(json.dumps({"audio": str(audio), "config": config}))
        # Runtime logs can include decoded text; keep them private, outside errors.
        with (root / "runtime.log").open("w+") as log:
            result = subprocess.run(
                [
                    python or sys.executable,
                    "-m",
                    "localplaud.asr.speech_runtime",
                    kind,
                    str(request),
                    str(output),
                ],
                stdout=log,
                stderr=log,
                timeout=timeout,
                check=False,
            )
        if result.returncode:
            error = json.loads(output.read_text()).get("error") if output.exists() else None
            raise AsrError(error or f"{kind} GPU process failed (exit {result.returncode})")
        return json.loads(output.read_text())


class QwenProvider:
    name = "qwen"

    def __init__(self, cfg):
        self.cfg = cfg

    def available(self):
        import shutil

        return bool(shutil.which(self.cfg.qwen.python or sys.executable))

    def transcribe(self, audio_path: Path, language: str = "auto") -> Transcript:
        result = run_speech_process(
            "qwen",
            audio_path,
            self.cfg.model_dump(mode="json", include={"qwen", "vad", "language"}),
            self.cfg.qwen.python,
            self.cfg.qwen.timeout_seconds,
        )
        segments = [
            Segment(**{**item, "words": [Word(**w) for w in item["words"]]})
            for item in result.pop("segments")
        ]
        return Transcript(segments=segments, **result)


@register("qwen")
def factory(cfg):
    return QwenProvider(cfg)
