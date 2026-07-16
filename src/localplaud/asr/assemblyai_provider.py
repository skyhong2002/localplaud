"""ASR via AssemblyAI — cloud, with server-side speaker labels."""

from __future__ import annotations

import logging
from pathlib import Path

from .base import AsrError, AsrUnavailable, Segment, Transcript, Word
from .registry import register

log = logging.getLogger(__name__)


def _speaker_label(raw) -> str | None:
    """Normalise AssemblyAI's "A"/"B" labels to "SPEAKER_00"-style names."""
    if raw is None:
        return None
    raw = str(raw)
    if len(raw) == 1 and "A" <= raw <= "Z":
        return f"SPEAKER_{ord(raw) - 65:02d}"
    return raw


class AssemblyAIProvider:
    name = "assemblyai"

    def __init__(self, cfg):
        self.cfg = cfg.assemblyai
        self.language = cfg.language

    def available(self) -> bool:
        return bool(self.cfg.api_key)

    def transcribe(self, audio_path: Path, language: str = "auto") -> Transcript:
        if not self.cfg.api_key:
            raise AsrUnavailable("AssemblyAI api_key is not set")
        try:
            import assemblyai as aai
        except ImportError as exc:
            raise AsrUnavailable("assemblyai package is not installed") from exc

        aai.settings.api_key = self.cfg.api_key
        config = aai.TranscriptionConfig(
            speaker_labels=self.cfg.speaker_labels,
            **(
                {"language_detection": True}
                if language == "auto"
                else {"language_code": language}
            ),
        )
        log.info("Transcribing with AssemblyAI (speaker_labels=%s)", self.cfg.speaker_labels)
        try:
            transcript = aai.Transcriber().transcribe(str(audio_path), config)
        except AsrError:
            raise
        except Exception as exc:
            raise AsrError(f"AssemblyAI transcription failed: {exc}") from exc
        if transcript.status == aai.TranscriptStatus.error:
            raise AsrError(f"AssemblyAI transcription failed: {transcript.error}")

        segments = []
        for utt in transcript.utterances or []:
            speaker = _speaker_label(utt.speaker)
            segments.append(
                Segment(
                    text=utt.text,
                    start=utt.start / 1000,
                    end=utt.end / 1000,
                    speaker=speaker,
                    words=[
                        Word(
                            text=w.text,
                            start=w.start / 1000,
                            end=w.end / 1000,
                            speaker=_speaker_label(getattr(w, "speaker", None)) or speaker,
                            confidence=getattr(w, "confidence", None),
                        )
                        for w in (utt.words or [])
                    ],
                )
            )

        duration = getattr(transcript, "audio_duration", None)
        model = getattr(transcript, "speech_model_used", None) or getattr(
            transcript, "speech_model", None
        )
        model = getattr(model, "value", model)
        return Transcript(
            segments=segments,
            language=None if language == "auto" else language,
            duration=float(duration) if duration is not None else None,
            provider=self.name,
            model=str(model) if model is not None else None,
            has_speakers=True,
        )


@register("assemblyai")
def _factory(cfg):
    return AssemblyAIProvider(cfg)
