"""ASR via the OpenAI audio transcription API (or a compatible base_url)."""

from __future__ import annotations

import logging
from pathlib import Path

from .base import AsrError, AsrUnavailable, Segment, Transcript, Word
from .registry import register

log = logging.getLogger(__name__)


def _wav_duration(path: Path) -> float | None:
    if path.suffix.lower() != ".wav":
        return None
    try:
        import wave

        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    except (OSError, wave.Error, ZeroDivisionError):
        return None


class OpenAIProvider:
    name = "openai"

    def __init__(self, cfg):
        self.cfg = cfg.openai
        self.language = cfg.language

    def available(self) -> bool:
        return bool(self.cfg.api_key)

    def transcribe(self, audio_path: Path, language: str = "auto") -> Transcript:
        if not self.cfg.api_key:
            raise AsrUnavailable("OpenAI ASR api_key is not set")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise AsrUnavailable("openai package is not installed") from exc

        if self.cfg.max_input_bytes is not None and audio_path.stat().st_size > self.cfg.max_input_bytes:
            raise AsrError("OpenAI ASR input exceeds configured max_input_bytes")
        client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url or None,
                        default_headers=self.cfg.headers or None,
                        timeout=self.cfg.timeout_seconds)
        log.info("Transcribing with OpenAI model %s", self.cfg.model)
        try:
            with open(audio_path, "rb") as fh:
                kwargs = {}
                if language != "auto":
                    kwargs["language"] = language
                if self.cfg.model == "whisper-1":
                    kwargs |= {
                        "response_format": "verbose_json",
                        "timestamp_granularities": ["segment", "word"],
                    }
                elif "diarize" in self.cfg.model:
                    kwargs |= {
                        "response_format": "diarized_json",
                        "chunking_strategy": "auto",
                    }
                else:
                    # GPT-4o transcription models support json/text, not
                    # whisper-1's verbose timestamp response.
                    kwargs["response_format"] = "json"
                resp = client.audio.transcriptions.create(
                    model=self.cfg.model,
                    file=fh,
                    **kwargs,
                )
        except AsrError:
            raise
        except Exception as exc:
            raise AsrError(f"OpenAI transcription failed: {exc}") from exc

        raw_segments = getattr(resp, "segments", None) or []
        segments = [
            Segment(text=seg.text, start=seg.start, end=seg.end,
                    speaker=getattr(seg, "speaker", None))
            for seg in raw_segments
        ]
        # The API returns words as a flat list; attach each word to the segment
        # whose time span contains its midpoint.
        for w in getattr(resp, "words", None) or []:
            mid = (w.start + w.end) / 2
            for seg in segments:
                if seg.start <= mid <= seg.end:
                    seg.words.append(Word(text=w.word, start=w.start, end=w.end))
                    break

        duration = getattr(resp, "duration", None) or _wav_duration(audio_path)
        if not segments and getattr(resp, "text", None):
            # Preserve the transcript without inventing timestamp precision.
            # The durable alignment stage will surface this as degraded.
            segments = [Segment(text=resp.text, start=0.0, end=duration or 0.0)]

        return Transcript(
            segments=segments,
            language=getattr(resp, "language", None),
            duration=duration,
            provider=self.name,
            model=self.cfg.model,
            has_speakers=any(segment.speaker for segment in segments),
        )


@register("openai")
def _factory(cfg):
    return OpenAIProvider(cfg)
