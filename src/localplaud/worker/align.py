"""Word-level alignment providers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..asr.base import Segment, Transcript, Word
from ..asr.remote_worker_provider import _parse_transcript
from ..config import AlignConfig
from ..providers.remote_inference import run_remote_audio_json


class AlignmentUnavailable(RuntimeError):
    pass


def align(audio: Path, transcript: Transcript, config: AlignConfig,
          remote_input=None) -> tuple[Transcript, dict]:
    if config.provider == "none":
        raise AlignmentUnavailable("alignment provider is disabled")
    if config.provider == "whisper-timestamps":
        words = [word for segment in transcript.segments for word in segment.words]
        if not words:
            raise AlignmentUnavailable(
                "ASR returned no word timestamps; select whisperx or a word-timestamp ASR provider"
            )
        if any(word.end < word.start or word.start < 0 for word in words):
            raise AlignmentUnavailable("ASR returned invalid word timestamp ranges")
        return transcript, {"method": "asr-word-timestamps", "words": len(words),
                            "forced": False}
    if config.provider == "remote-worker":
        if remote_input is None:
            raise AlignmentUnavailable("remote input signing is not configured")
        try:
            data = run_remote_audio_json(config, remote_input, audio, "alignment",
                                         {"transcript": _payload(transcript)})
            result = _parse_transcript(data, config.model or "remote-alignment")
            return result, {"method": "remote-worker", "forced": True,
                            "words": sum(len(row.words) for row in result.segments)}
        except Exception as exc:
            raise AlignmentUnavailable(f"remote alignment failed: {exc}") from exc
    if config.provider != "whisperx":
        raise AlignmentUnavailable(f"unknown alignment provider: {config.provider}")
    try:
        import whisperx
    except ImportError as exc:
        raise AlignmentUnavailable("WhisperX is not installed; install the alignment extra") from exc
    language = transcript.language
    if not language or language == "auto":
        raise AlignmentUnavailable("WhisperX alignment requires a detected transcript language")
    device = config.device if config.device != "auto" else "cpu"
    language_base = language.split("-", 1)[0].lower()
    model_name = (
        config.model
        or config.models_by_language.get(language)
        or config.models_by_language.get(language_base)
        or config.models_by_language.get("default")
    )
    try:
        model, metadata = whisperx.load_align_model(
            language_code=language, device=device, model_name=model_name
        )
        raw_segments = [{"text": row.text, "start": row.start, "end": row.end}
                        for row in transcript.segments]
        result = whisperx.align(raw_segments, model, metadata, str(audio), device,
                                return_char_alignments=False)
    except Exception as exc:  # noqa: BLE001
        raise AlignmentUnavailable(f"WhisperX alignment failed: {exc}") from exc
    segments = []
    for row in result.get("segments", []):
        words = [Word(text=item.get("word", ""), start=float(item.get("start", row["start"])),
                      end=float(item.get("end", row["end"])), confidence=item.get("score"))
                 for item in row.get("words", [])]
        segments.append(Segment(text=row.get("text", ""), start=float(row["start"]),
                                end=float(row["end"]), words=words))
    if not segments:
        raise AlignmentUnavailable("WhisperX returned no aligned segments")
    return replace(transcript, segments=segments), {
        "method": "whisperx", "forced": True,
        "words": sum(len(row.words) for row in segments), "language": language,
        "alignment_model": model_name or "whisperx-language-default",
    }


def _payload(transcript: Transcript) -> dict:
    return {"language": transcript.language, "duration": transcript.duration,
            "has_speakers": transcript.has_speakers,
            "segments": [{"text": row.text, "start": row.start, "end": row.end,
                          "speaker": row.speaker,
                          "words": [{"text": word.text, "start": word.start,
                                     "end": word.end, "speaker": word.speaker,
                                     "confidence": word.confidence}
                                    for word in row.words]}
                         for row in transcript.segments]}
