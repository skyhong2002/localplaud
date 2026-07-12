"""Word-level timestamp validation and optional WhisperX forced alignment."""

from __future__ import annotations

import importlib.metadata
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..asr.base import Segment, Transcript, Word

PROVIDER_TIMESTAMPS = "provider-word-timestamps"
WHISPERX_PROVIDER = "whisperx"
WHISPERX_AUTO_MODEL = "wav2vec2-auto"
_TIMESTAMP_PROVIDERS = {
    PROVIDER_TIMESTAMPS,
    "assemblyai",
    "deepgram",
    "faster-whisper",
    "mlx-whisper",
    "openai",
    "remote-worker",
}


class AlignmentError(RuntimeError):
    """Alignment executed but returned invalid or incomplete timing evidence."""


class AlignmentUnavailable(AlignmentError):
    """The selected alignment runtime or required input is unavailable."""


@dataclass(frozen=True)
class AlignmentResult:
    transcript: Transcript
    provider: str
    model: str | None
    detail: dict[str, Any]


def inspect_word_alignment(transcript: Transcript) -> dict[str, Any]:
    words = [word for segment in transcript.segments for word in segment.words]
    if not words:
        raise AlignmentUnavailable(
            "ASR provider returned segment timestamps but no word timestamps"
        )
    previous_segment_start = -1.0
    previous_word_start = -1.0
    timed_segments = 0
    for segment_index, segment in enumerate(transcript.segments):
        if not math.isfinite(segment.start) or not math.isfinite(segment.end):
            raise AlignmentError(f"segment {segment_index} has a non-finite timestamp")
        if segment.start < 0 or segment.end < segment.start:
            raise AlignmentError(f"segment {segment_index} has an invalid timestamp range")
        if segment.start < previous_segment_start:
            raise AlignmentError(f"segment {segment_index} is not chronologically ordered")
        previous_segment_start = segment.start
        if segment.words:
            timed_segments += 1
        for word_index, word in enumerate(segment.words):
            label = f"word {word_index} in segment {segment_index}"
            if not math.isfinite(word.start) or not math.isfinite(word.end):
                raise AlignmentError(f"{label} has a non-finite timestamp")
            if word.start < 0 or word.end < word.start:
                raise AlignmentError(f"{label} has an invalid timestamp range")
            if word.start < previous_word_start:
                raise AlignmentError(f"{label} is not chronologically ordered")
            if word.start < segment.start - 0.05 or word.end > segment.end + 0.05:
                raise AlignmentError(f"{label} falls outside its segment")
            if word.confidence is not None and (
                not math.isfinite(word.confidence) or not 0 <= word.confidence <= 1
            ):
                raise AlignmentError(f"{label} has an invalid confidence")
            previous_word_start = word.start
    return {
        "strategy": PROVIDER_TIMESTAMPS,
        "forced_alignment": False,
        "word_count": len(words),
        "timed_segments": timed_segments,
        "segment_count": len(transcript.segments),
        "segment_coverage": (
            timed_segments / len(transcript.segments) if transcript.segments else 0.0
        ),
    }


def _import_whisperx():
    try:
        import whisperx
    except Exception as exc:  # noqa: BLE001 - optional native stack can fail broadly
        raise AlignmentUnavailable(
            "WhisperX is unavailable; install the 'forced-align' extra"
        ) from exc
    return whisperx


def _resolve_device(requested: str) -> str:
    if requested not in {"auto", "cpu", "cuda"}:
        raise AlignmentUnavailable(f"unsupported WhisperX alignment device: {requested}")
    if requested == "cpu":
        return "cpu"
    try:
        import torch
    except Exception as exc:  # noqa: BLE001 - torch import can fail at binary load time
        raise AlignmentUnavailable("PyTorch is unavailable for WhisperX alignment") from exc
    cuda = bool(torch.cuda.is_available())
    if requested == "cuda" and not cuda:
        raise AlignmentUnavailable(
            "CUDA requested for WhisperX alignment but torch.cuda.is_available() is false"
        )
    return "cuda" if cuda else "cpu"


def health(
    provider: str = WHISPERX_PROVIDER,
    model: str | None = WHISPERX_AUTO_MODEL,
    options: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if provider != WHISPERX_PROVIDER:
        return True, "provider word timestamps will be validated without forced alignment"
    try:
        _import_whisperx()
        device = _resolve_device(str((options or {}).get("device", "auto")))
    except AlignmentUnavailable as exc:
        return False, str(exc)
    selected = model or WHISPERX_AUTO_MODEL
    return True, (
        f"WhisperX runtime available on {device}; {selected} resolves and downloads "
        "the language-specific model on first alignment"
    )


def _language_code(language: str | None) -> str:
    value = (language or "").strip().lower().replace("_", "-")
    if not value or value == "auto":
        raise AlignmentUnavailable(
            "WhisperX forced alignment requires the ASR transcript language"
        )
    return value.split("-", 1)[0]


def _whisperx_version() -> str:
    try:
        return importlib.metadata.version("whisperx")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _forced_align_whisperx(
    audio_path: Path,
    transcript: Transcript,
    *,
    model: str | None,
    options: dict[str, Any],
) -> AlignmentResult:
    whisperx = _import_whisperx()
    device = _resolve_device(str(options.get("device", "auto")))
    language = _language_code(transcript.language)
    interpolate = str(options.get("interpolate_method", "nearest"))
    if interpolate not in {"nearest", "linear", "ignore"}:
        raise AlignmentUnavailable(
            f"unsupported WhisperX interpolate_method: {interpolate}"
        )
    try:
        minimum_coverage = float(options.get("min_segment_coverage", 0.8))
    except (TypeError, ValueError) as exc:
        raise AlignmentUnavailable("min_segment_coverage must be a number") from exc
    if not 0 <= minimum_coverage <= 1:
        raise AlignmentUnavailable("min_segment_coverage must be between 0 and 1")
    source_segments = [
        {"text": segment.text, "start": segment.start, "end": segment.end}
        for segment in transcript.segments
    ]
    if not source_segments or not any(item["text"].strip() for item in source_segments):
        raise AlignmentUnavailable("WhisperX forced alignment requires transcript segments")

    requested_model = None if model in {None, "auto", WHISPERX_AUTO_MODEL} else model
    load_kwargs: dict[str, Any] = {"language_code": language, "device": device}
    if requested_model:
        load_kwargs["model_name"] = requested_model
    if options.get("model_dir"):
        load_kwargs["model_dir"] = str(options["model_dir"])
    try:
        align_model, metadata = whisperx.load_align_model(**load_kwargs)
        audio = whisperx.load_audio(str(audio_path))
        payload = whisperx.align(
            source_segments,
            align_model,
            metadata,
            audio,
            device,
            interpolate_method=interpolate,
            return_char_alignments=bool(options.get("return_char_alignments", False)),
        )
    except AlignmentError:
        raise
    except Exception as exc:  # noqa: BLE001 - model/audio runtimes fail with varied types
        raise AlignmentUnavailable(f"WhisperX forced alignment failed: {exc}") from exc

    aligned_segments = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(aligned_segments, list) or len(aligned_segments) != len(transcript.segments):
        raise AlignmentError(
            "WhisperX returned a different segment count; refusing to replace transcript timing"
        )
    segments: list[Segment] = []
    unaligned_words = 0
    for index, (source, aligned) in enumerate(zip(transcript.segments, aligned_segments, strict=True)):
        if not isinstance(aligned, dict):
            raise AlignmentError(f"WhisperX segment {index} is not an object")
        words: list[Word] = []
        for item in aligned.get("words") or []:
            if not isinstance(item, dict):
                continue
            word_text = str(item.get("word", item.get("text", "")))
            start, end = item.get("start"), item.get("end")
            if not word_text.strip() or start is None or end is None:
                unaligned_words += 1
                continue
            words.append(
                Word(
                    text=word_text,
                    start=float(start),
                    end=float(end),
                    speaker=item.get("speaker") or source.speaker,
                    confidence=(
                        float(item["score"]) if item.get("score") is not None else None
                    ),
                )
            )
        segment_start = float(aligned.get("start", source.start))
        segment_end = float(aligned.get("end", source.end))
        segments.append(
            Segment(
                text=source.text,
                start=segment_start,
                end=segment_end,
                speaker=aligned.get("speaker") or source.speaker,
                words=words,
            )
        )

    result = Transcript(
        segments=segments,
        language=transcript.language,
        duration=transcript.duration,
        provider=transcript.provider,
        model=transcript.model,
        has_speakers=transcript.has_speakers,
    )
    detail = inspect_word_alignment(result)
    if detail["segment_coverage"] < minimum_coverage:
        raise AlignmentError(
            "WhisperX aligned segment coverage "
            f"{detail['segment_coverage']:.1%} is below required {minimum_coverage:.1%}"
        )
    detail |= {
        "strategy": "whisperx-wav2vec2",
        "forced_alignment": True,
        "provider": WHISPERX_PROVIDER,
        "alignment_model": requested_model or WHISPERX_AUTO_MODEL,
        "implementation_version": _whisperx_version(),
        "device": device,
        "language": language,
        "interpolate_method": interpolate,
        "minimum_segment_coverage": minimum_coverage,
        "unaligned_words": unaligned_words,
    }
    return AlignmentResult(result, WHISPERX_PROVIDER, model or WHISPERX_AUTO_MODEL, detail)


def run_alignment(
    audio_path: Path,
    transcript: Transcript,
    *,
    provider: str,
    model: str | None,
    options: dict[str, Any] | None = None,
) -> AlignmentResult:
    """Dispatch the resolved alignment selection without implicit provider changes."""
    options = dict(options or {})
    if provider == WHISPERX_PROVIDER:
        return _forced_align_whisperx(audio_path, transcript, model=model, options=options)
    if provider not in _TIMESTAMP_PROVIDERS:
        raise AlignmentUnavailable(f"unsupported alignment provider: {provider}")
    detail = inspect_word_alignment(transcript)
    return AlignmentResult(transcript, transcript.provider or provider, transcript.model, detail)


def selection_uses_forced_alignment(selection: dict[str, Any] | None) -> bool:
    if not selection:
        return False
    provider = selection.get("provider_type") or str(
        selection.get("connection", "")
    ).split(":", 1)[-1]
    return provider == WHISPERX_PROVIDER
