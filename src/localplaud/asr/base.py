"""ASR provider interface and the shared transcript data model.

Every provider — local or cloud — takes an audio file and returns a
``Transcript``. Some providers (Deepgram, AssemblyAI, WhisperX) also return
speaker labels; when they don't, the diarization stage fills them in. This is
the single contract the rest of the pipeline depends on, so all providers
normalise to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class Word:
    text: str
    start: float  # seconds from start of audio
    end: float
    speaker: str | None = None  # e.g. "SPEAKER_00", filled by diarization
    confidence: float | None = None


@dataclass
class Segment:
    """A contiguous chunk of speech, typically one utterance."""

    text: str
    start: float
    end: float
    speaker: str | None = None
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    segments: list[Segment]
    language: str | None = None
    duration: float | None = None  # seconds
    provider: str = ""
    model: str | None = None
    # True if the segments already carry speaker labels (cloud diarization or
    # WhisperX) and the local diarization stage can be skipped.
    has_speakers: bool = False

    @property
    def text(self) -> str:
        return "\n".join(s.text.strip() for s in self.segments if s.text.strip())

    @property
    def speakers(self) -> list[str]:
        seen: list[str] = []
        for s in self.segments:
            if s.speaker and s.speaker not in seen:
                seen.append(s.speaker)
        return seen


class AsrError(RuntimeError):
    """Raised when a provider fails to transcribe."""


class AsrUnavailable(AsrError):
    """Raised when a provider can't run in this environment (missing GPU,
    model, dependency, or API key). Triggers fallback to the next provider."""


@runtime_checkable
class AsrProvider(Protocol):
    """Contract for all ASR providers.

    Implementations are constructed from their config sub-model and register
    themselves in ``localplaud.asr.registry``.
    """

    name: str

    def available(self) -> bool:
        """Cheap check: can this provider run here right now (deps present,
        API key set, model reachable)? Used to decide fallback without paying
        for a full transcription attempt."""
        ...

    def transcribe(self, audio_path: Path, language: str = "auto") -> Transcript:
        """Transcribe ``audio_path``. Raise :class:`AsrUnavailable` to fall
        back to the next provider, :class:`AsrError` for a hard failure."""
        ...
