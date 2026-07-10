"""ASR via faster-whisper (CTranslate2 Whisper) — local, CPU or CUDA."""

from __future__ import annotations

import logging
from pathlib import Path

from .base import AsrError, AsrUnavailable, Segment, Transcript, Word
from .registry import register

log = logging.getLogger(__name__)


class FasterWhisperProvider:
    name = "faster-whisper"

    def __init__(self, cfg):
        self.cfg = cfg.faster_whisper
        self.vad = cfg.vad
        self.language = cfg.language

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    def _resolve_device(self) -> tuple[str, str]:
        device = self.cfg.device
        if device == "auto":
            try:
                import torch

                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"
        compute_type = self.cfg.compute_type
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        return device, compute_type

    def transcribe(self, audio_path: Path, language: str = "auto") -> Transcript:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise AsrUnavailable("faster-whisper is not installed") from exc

        device, compute_type = self._resolve_device()
        log.info(
            "Loading faster-whisper model %s (device=%s, compute_type=%s)",
            self.cfg.model, device, compute_type,
        )
        # faster-whisper bundles its own silero VAD, so vad_filter needs no extra
        # dependency. Off by default until benchmarked on real recordings.
        vad_kwargs: dict = {}
        if self.vad.enabled:
            log.info("faster-whisper VAD filter enabled")
            vad_kwargs["vad_filter"] = True
            vad_kwargs["vad_parameters"] = {
                "threshold": self.vad.threshold,
                "min_speech_duration_ms": self.vad.min_speech_ms,
                "min_silence_duration_ms": self.vad.min_silence_ms,
                "speech_pad_ms": self.vad.speech_pad_ms,
                "max_speech_duration_s": self.vad.max_region_s,
            }
        try:
            model = WhisperModel(self.cfg.model, device=device, compute_type=compute_type)
            segments_iter, info = model.transcribe(
                str(audio_path),
                language=None if language == "auto" else language,
                word_timestamps=True,
                **vad_kwargs,
            )
            segments = [
                Segment(
                    text=seg.text,
                    start=seg.start,
                    end=seg.end,
                    words=[
                        Word(
                            text=w.word,
                            start=w.start,
                            end=w.end,
                            confidence=w.probability,
                        )
                        for w in (seg.words or [])
                    ],
                )
                for seg in segments_iter
            ]
        except AsrError:
            raise
        except Exception as exc:
            raise AsrError(f"faster-whisper transcription failed: {exc}") from exc

        return Transcript(
            segments=segments,
            language=info.language,
            duration=info.duration,
            provider=self.name,
            model=self.cfg.model,
            has_speakers=False,
        )


@register("faster-whisper")
def _factory(cfg):
    return FasterWhisperProvider(cfg)
