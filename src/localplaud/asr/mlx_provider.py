"""ASR via mlx-whisper — local, Apple Silicon (MLX) only."""

from __future__ import annotations

import logging
from pathlib import Path

from .base import AsrError, AsrUnavailable, Segment, Transcript, Word
from .registry import register

log = logging.getLogger(__name__)


class MlxWhisperProvider:
    name = "mlx-whisper"

    def __init__(self, cfg):
        self.cfg = cfg.mlx_whisper
        self.language = cfg.language

    def available(self) -> bool:
        try:
            import mlx_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    def transcribe(self, audio_path: Path, language: str = "auto") -> Transcript:
        try:
            import mlx_whisper
        except ImportError as exc:
            raise AsrUnavailable("mlx-whisper is not installed") from exc

        log.info("Transcribing with mlx-whisper model %s", self.cfg.model)
        try:
            result = mlx_whisper.transcribe(
                str(audio_path),
                path_or_hf_repo=self.cfg.model,
                word_timestamps=True,
                language=None if language == "auto" else language,
            )
        except Exception as exc:
            raise AsrError(f"mlx-whisper transcription failed: {exc}") from exc

        segments = [
            Segment(
                text=seg.get("text", ""),
                start=seg.get("start", 0.0),
                end=seg.get("end", 0.0),
                words=[
                    Word(
                        text=w.get("word", ""),
                        start=w.get("start", 0.0),
                        end=w.get("end", 0.0),
                        confidence=w.get("probability"),
                    )
                    for w in seg.get("words", [])
                ],
            )
            for seg in result.get("segments", [])
        ]

        return Transcript(
            segments=segments,
            language=result.get("language"),
            duration=segments[-1].end if segments else None,
            provider=self.name,
            model=self.cfg.model,
            has_speakers=False,
        )


@register("mlx-whisper")
def _factory(cfg):
    return MlxWhisperProvider(cfg)
