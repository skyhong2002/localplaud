"""Qwen transcription and forced alignment in a disposable GPU process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .base import AsrError, Segment, Transcript, Word
from .registry import register

FAILURE_LOG_DIR = Path("data/speech-runtime-failures")
FAILURE_LOG_KEEP = 20


def _keep_failure_log(kind: str, runtime_log: Path) -> Path | None:
    """Preserve a failed runtime's private log so the real cause stays diagnosable.

    The stage error shown in the Web App is deliberately sanitized to an
    exception name; without this copy that name was the only evidence left.
    Logs may contain decoded text, so they stay owner-readable on the machine
    that ran the model and are rotated to the newest ``FAILURE_LOG_KEEP``.
    """
    directory = FAILURE_LOG_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%f")
        target = directory / f"{stamp}-{kind}-{os.getpid()}.log"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(runtime_log.read_bytes()[-_FAILURE_LOG_TAIL_BYTES:])
        for stale in sorted(directory.glob("*.log"))[:-FAILURE_LOG_KEEP]:
            stale.unlink(missing_ok=True)
        return target
    except OSError:
        return None


_FAILURE_LOG_TAIL_BYTES = 256 * 1024


def run_speech_process(kind: str, audio: Path, config: dict, python: str, timeout: int):
    with tempfile.TemporaryDirectory(prefix="localplaud-speech-") as directory:
        root = Path(directory)
        request, output = root / "request.json", root / "result.json"
        request.write_text(json.dumps({"audio": str(audio), "config": config}))
        # Runtime logs can include decoded text; keep them private, outside errors.
        runtime_log = root / "runtime.log"
        with runtime_log.open("w+") as log:
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
            message = error or f"{kind} GPU process failed (exit {result.returncode})"
            saved = _keep_failure_log(kind, runtime_log)
            if saved is not None:
                message += f"; runtime log kept on the speech worker as {saved.name}"
            raise AsrError(message)
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
