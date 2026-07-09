"""Transcription stage — run ASR (with fallback) and persist the result."""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from ..asr.base import Transcript as AsrTranscript
from ..asr.registry import transcribe_with_fallback
from ..config import Settings

log = logging.getLogger(__name__)


def run_asr(wav: Path, settings: Settings) -> AsrTranscript:
    return transcribe_with_fallback(wav, settings.asr)


def segments_to_json(transcript: AsrTranscript) -> list[dict]:
    return [asdict(seg) for seg in transcript.segments]
