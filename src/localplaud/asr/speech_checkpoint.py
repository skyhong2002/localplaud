"""Private, content-addressed Qwen chunk results for interrupted long recordings."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


class SpeechCheckpoint:
    def __init__(self, audio: Path, cfg: dict):
        root = cfg["qwen"].get("checkpoint_dir", "data/speech-checkpoints")
        self.directory = None
        if not root:
            return
        identity = {
            "algorithm": "qwen-vad-bisection-v1",
            "language": cfg["language"],
            "vad": cfg["vad"],
            "qwen": {
                k: v
                for k, v in cfg["qwen"].items()
                if k not in {"python", "timeout_seconds", "checkpoint_dir"}
            },
        }
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode())
        with audio.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        base = Path(root)
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = base / digest.hexdigest()
        self.directory.mkdir(exist_ok=True, mode=0o700)

    def _path(self, start, end):
        key = hashlib.sha256(json.dumps([start, end]).encode()).hexdigest()
        return self.directory / f"{key}.json" if self.directory else None

    def read(self, start, end):
        path = self._path(start, end)
        if path is None:
            return None
        try:
            result = json.loads(path.read_text())
            if result.get("token_limited") is True:
                return result
            if isinstance(result.get("transcription"), str) and isinstance(
                result.get("language"), str
            ):
                return result
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def write(self, start, end, result):
        path = self._path(start, end)
        if path is None:
            return
        pending = path.with_suffix(".tmp")
        with pending.open("w", encoding="utf-8") as stream:
            os.chmod(pending, 0o600)
            json.dump(result, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(path)
