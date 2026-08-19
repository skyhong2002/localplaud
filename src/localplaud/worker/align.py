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
# WhisperX pads sub-25 ms input to 400 samples, but wav2vec2 still produces a
# one-frame trellis and divides by ``trellis.size(0) - 1``. Keep a small safety
# margin above the roughly 45 ms required for two frames at 16 kHz.
_WHISPERX_MIN_SEGMENT_SECONDS = 0.05
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
    previous_global_word_start = -1.0
    cross_segment_word_overlaps = 0
    timed_segments = 0
    for segment_index, segment in enumerate(transcript.segments):
        if not math.isfinite(segment.start) or not math.isfinite(segment.end):
            raise AlignmentError(f"segment {segment_index} has a non-finite timestamp")
        if segment.start < 0 or segment.end < segment.start:
            raise AlignmentError(f"segment {segment_index} has an invalid timestamp range")
        # Some Whisper providers emit content-free bookkeeping placeholders at
        # chunk boundaries. Their stale timestamp can fall behind the adjacent
        # speech segments even though they carry no text or words. Preserve and
        # validate the placeholder, but do not let it corrupt speech chronology.
        empty_placeholder = not segment.text.strip() and not segment.words
        if not empty_placeholder:
            if segment.start < previous_segment_start:
                raise AlignmentError(f"segment {segment_index} is not chronologically ordered")
            previous_segment_start = segment.start
        if segment.words:
            timed_segments += 1
        previous_word_start = -1.0
        for word_index, word in enumerate(segment.words):
            label = f"word {word_index} in segment {segment_index}"
            if not math.isfinite(word.start) or not math.isfinite(word.end):
                raise AlignmentError(f"{label} has a non-finite timestamp")
            if word.start < 0 or word.end < word.start:
                raise AlignmentError(f"{label} has an invalid timestamp range")
            if word.start < previous_word_start:
                raise AlignmentError(f"{label} is not chronologically ordered")
            if word.start < previous_global_word_start:
                # Adjacent ASR chunks can legitimately overlap in time. Keep
                # strict ordering inside each segment, while recording rather
                # than rejecting cross-segment overlap.
                cross_segment_word_overlaps += 1
            if word.start < segment.start - 0.05 or word.end > segment.end + 0.05:
                raise AlignmentError(f"{label} falls outside its segment")
            if word.confidence is not None and (
                not math.isfinite(word.confidence) or not 0 <= word.confidence <= 1
            ):
                raise AlignmentError(f"{label} has an invalid confidence")
            previous_word_start = word.start
            previous_global_word_start = max(previous_global_word_start, word.start)
    detail = {
        "strategy": PROVIDER_TIMESTAMPS,
        "forced_alignment": False,
        "word_count": len(words),
        "timed_segments": timed_segments,
        "segment_count": len(transcript.segments),
        "segment_coverage": (
            timed_segments / len(transcript.segments) if transcript.segments else 0.0
        ),
    }
    if cross_segment_word_overlaps:
        detail["cross_segment_word_overlaps"] = cross_segment_word_overlaps
    return detail


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
    source_segments = []
    skipped_empty_segments = 0
    skipped_short_segment_indexes: set[int] = set()
    for index, segment in enumerate(transcript.segments):
        if not segment.text.strip():
            # MLX Whisper can emit empty, zero-duration placeholders between
            # speech chunks. WhisperX divides by segment duration internally,
            # so preserve these in the transcript but never send them to the
            # aligner.
            skipped_empty_segments += 1
            continue
        if segment.end <= segment.start:
            raise AlignmentError(
                f"input segment {index} has text but no positive duration"
            )
        if segment.end - segment.start < _WHISPERX_MIN_SEGMENT_SECONDS:
            # Preserve extremely short ASR fragments as unaligned evidence.
            # Sending them to WhisperX can create a one-frame trellis whose
            # timestamp conversion divides by zero.
            skipped_short_segment_indexes.add(index)
            continue
        source_segments.append(
            {
                "text": segment.text,
                "start": segment.start,
                "end": segment.end,
                # Older WhisperX versions preserve avg_logprob while splitting
                # an input segment. Newer releases are mapped by timestamps.
                "avg_logprob": float(index),
            }
        )
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
    if not isinstance(aligned_segments, list):
        raise AlignmentError("WhisperX returned no aligned segments")
    grouped: dict[int, list[dict[str, Any]]] = {
        index: [] for index in range(len(transcript.segments))
    }
    timestamp_mapped_segments = 0
    nearest_mapped_segments = 0
    alignable_source_indexes = {
        int(item["avg_logprob"]) for item in source_segments
    }
    for aligned in aligned_segments:
        if not isinstance(aligned, dict):
            raise AlignmentError("WhisperX returned a non-object segment")
        marker = aligned.get("avg_logprob")
        if (
            isinstance(marker, int | float)
            and float(marker).is_integer()
            and int(marker) in grouped
        ):
            source_index = int(marker)
        else:
            # WhisperX 3.7+ no longer preserves arbitrary input keys such as
            # avg_logprob. Map its sentence-level output back to the immutable
            # ASR segment by timestamp overlap; the source text itself is never
            # replaced by WhisperX output.
            try:
                aligned_start = float(aligned["start"])
                aligned_end = float(aligned["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise AlignmentError(
                    "WhisperX returned a segment without a usable source marker or timestamps"
                ) from exc
            if not math.isfinite(aligned_start) or not math.isfinite(aligned_end):
                raise AlignmentError("WhisperX returned non-finite segment timestamps")
            if aligned_end < aligned_start:
                raise AlignmentError("WhisperX returned an invalid segment timestamp range")
            overlaps = {
                index: max(
                    0.0,
                    min(aligned_end, transcript.segments[index].end)
                    - max(aligned_start, transcript.segments[index].start),
                )
                for index in alignable_source_indexes
            }
            source_index, best_overlap = max(
                overlaps.items(), key=lambda item: item[1], default=(-1, 0.0)
            )
            if best_overlap <= 0:
                # Sentence tokenization can yield a point timestamp, and CTC
                # alignment may drift just outside the ASR segment boundary.
                # Map only to a nearby segment that was actually sent to
                # WhisperX; a larger discrepancy remains a hard failure.
                gaps = {
                    index: max(
                        transcript.segments[index].start - aligned_end,
                        aligned_start - transcript.segments[index].end,
                        0.0,
                    )
                    for index in alignable_source_indexes
                }
                source_index, best_gap = min(
                    gaps.items(), key=lambda item: item[1], default=(-1, math.inf)
                )
                if best_gap > 0.5:
                    raise AlignmentError(
                        "WhisperX output could not be mapped to a nearby input segment"
                    )
                nearest_mapped_segments += 1
            timestamp_mapped_segments += 1
        grouped[source_index].append(aligned)

    segments: list[Segment] = []
    unaligned_words = 0
    unaligned_segments = 0
    for index, source in enumerate(transcript.segments):
        parts = grouped[index]
        if not parts:
            if not source.text.strip():
                segments.append(source)
                continue
            if index in skipped_short_segment_indexes:
                unaligned_segments += 1
                segments.append(source)
                continue
            raise AlignmentError(f"WhisperX omitted input segment {index}")
        words: list[Word] = []
        for part in parts:
            for item in part.get("words") or []:
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
        if not words:
            unaligned_segments += 1
        starts = [float(part["start"]) for part in parts if part.get("start") is not None]
        ends = [float(part["end"]) for part in parts if part.get("end") is not None]
        segment_start = min(starts, default=source.start)
        segment_end = max(ends, default=source.end)
        segments.append(
            Segment(
                text=source.text,
                start=segment_start,
                end=segment_end,
                speaker=source.speaker,
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
    alignable_segment_count = len(transcript.segments) - skipped_empty_segments
    aligned_segment_count = sum(
        bool(segment.words) and bool(segment.text.strip()) for segment in result.segments
    )
    detail["segment_coverage"] = (
        aligned_segment_count / alignable_segment_count if alignable_segment_count else 0.0
    )
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
        "unaligned_segments": unaligned_segments,
        "skipped_empty_segments": skipped_empty_segments,
        "skipped_short_segments": len(skipped_short_segment_indexes),
        "alignable_segment_count": alignable_segment_count,
    }
    if timestamp_mapped_segments:
        detail["timestamp_mapped_segments"] = timestamp_mapped_segments
    if nearest_mapped_segments:
        detail["nearest_mapped_segments"] = nearest_mapped_segments
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
