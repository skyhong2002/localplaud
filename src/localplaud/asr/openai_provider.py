"""ASR via the OpenAI audio transcription API (or a compatible base_url)."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import get_settings
from ..openai_budget import (
    OpenAIBudgetBlocked,
    assert_openai_free_pool,
    is_real_openai_base_url,
)
from .base import AsrError, AsrUnavailable, Segment, Transcript, Word
from .registry import register

log = logging.getLogger(__name__)


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

        if is_real_openai_base_url(self.cfg.base_url):
            try:
                assert_openai_free_pool(
                    get_settings(), model=self.cfg.model, projected_tokens=0
                )
            except OpenAIBudgetBlocked as exc:
                raise AsrError(str(exc)) from exc

        client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url or None)
        log.info("Transcribing with OpenAI model %s", self.cfg.model)
        try:
            with open(audio_path, "rb") as fh:
                kwargs = {}
                if language != "auto":
                    kwargs["language"] = language
                resp = client.audio.transcriptions.create(
                    model=self.cfg.model,
                    file=fh,
                    response_format="verbose_json",
                    timestamp_granularities=["segment", "word"],
                    **kwargs,
                )
        except AsrError:
            raise
        except Exception as exc:
            raise AsrError(f"OpenAI transcription failed: {exc}") from exc

        segments = [
            Segment(text=seg.text, start=seg.start, end=seg.end)
            for seg in (getattr(resp, "segments", None) or [])
        ]
        # The API returns words as a flat list; attach each word to the segment
        # whose time span contains its midpoint.
        for w in getattr(resp, "words", None) or []:
            mid = (w.start + w.end) / 2
            for seg in segments:
                if seg.start <= mid <= seg.end:
                    seg.words.append(Word(text=w.word, start=w.start, end=w.end))
                    break

        return Transcript(
            segments=segments,
            language=getattr(resp, "language", None),
            duration=getattr(resp, "duration", None),
            provider=self.name,
            model=self.cfg.model,
            has_speakers=False,
        )


@register("openai")
def _factory(cfg):
    return OpenAIProvider(cfg)
