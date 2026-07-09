"""ASR via Deepgram — cloud, with server-side speaker diarization."""

from __future__ import annotations

import logging
from pathlib import Path

from .base import AsrError, AsrUnavailable, Segment, Transcript, Word
from .registry import register

log = logging.getLogger(__name__)


def _get(obj, key, default=None):
    """Read ``key`` from an SDK object or a plain dict interchangeably."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class DeepgramProvider:
    name = "deepgram"

    def __init__(self, cfg):
        self.cfg = cfg.deepgram
        self.language = cfg.language

    def available(self) -> bool:
        return bool(self.cfg.api_key)

    def transcribe(self, audio_path: Path, language: str = "auto") -> Transcript:
        if not self.cfg.api_key:
            raise AsrUnavailable("Deepgram api_key is not set")
        try:
            from deepgram import DeepgramClient, PrerecordedOptions
        except ImportError as exc:
            raise AsrUnavailable("deepgram-sdk is not installed") from exc

        options = PrerecordedOptions(
            model=self.cfg.model,
            diarize=self.cfg.diarize,
            punctuate=True,
            utterances=True,
            smart_format=True,
            **({} if language == "auto" else {"language": language}),
        )
        log.info("Transcribing with Deepgram model %s", self.cfg.model)
        try:
            dg = DeepgramClient(self.cfg.api_key)
            with open(audio_path, "rb") as fh:
                payload = {"buffer": fh.read()}
            resp = dg.listen.rest.v("1").transcribe_file(payload, options)
        except AsrError:
            raise
        except Exception as exc:
            raise AsrError(f"Deepgram transcription failed: {exc}") from exc

        results = _get(resp, "results")
        utterances = _get(results, "utterances") or []

        segments = []
        for utt in utterances:
            speaker_id = _get(utt, "speaker")
            speaker = f"SPEAKER_{speaker_id:02d}" if speaker_id is not None else None
            words = []
            for w in _get(utt, "words") or []:
                w_speaker_id = _get(w, "speaker")
                words.append(
                    Word(
                        text=_get(w, "word", ""),
                        start=_get(w, "start", 0.0),
                        end=_get(w, "end", 0.0),
                        speaker=(
                            f"SPEAKER_{w_speaker_id:02d}"
                            if w_speaker_id is not None
                            else None
                        ),
                        confidence=_get(w, "confidence"),
                    )
                )
            segments.append(
                Segment(
                    text=_get(utt, "transcript", ""),
                    start=_get(utt, "start", 0.0),
                    end=_get(utt, "end", 0.0),
                    speaker=speaker,
                    words=words,
                )
            )

        return Transcript(
            segments=segments,
            language=None if language == "auto" else language,
            duration=segments[-1].end if segments else None,
            provider=self.name,
            model=self.cfg.model,
            has_speakers=self.cfg.diarize,
        )


@register("deepgram")
def _factory(cfg):
    return DeepgramProvider(cfg)
