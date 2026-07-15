"""Speaker diarization — assign speaker labels to a transcript.

Only runs when the ASR provider didn't already return speakers. Uses
pyannote.audio locally (needs a HuggingFace token to fetch the pipeline). The
diarization timeline is intersected with each word/segment: a segment gets the
speaker who overlaps it most.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..asr.base import Segment, Transcript, Word
from ..config import DiarizeConfig

log = logging.getLogger(__name__)
DEFAULT_SPEAKER_GROUP_GAP_SECONDS = 3.0
DEFAULT_SPEAKER_GROUP_MAX_CHARS = 1_200
DEFAULT_SPEAKER_GROUP_MAX_DURATION_SECONDS = 120.0


def _is_cjk(value: str) -> bool:
    codepoint = ord(value)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _join_text(left: str, right: str) -> str:
    left, right = left.rstrip(), right.lstrip()
    if not left:
        return right
    if not right:
        return left
    if (
        right[0] in ",.;:!?%)]}，。！？、；：」』】）》…"
        or left[-1] in "([{「『【《（-/'"
        or right[0] in "-/'"
        or (_is_cjk(left[-1]) and _is_cjk(right[0]))
        or (
            left[-1] in "，。！？、；：」』】）》…"
            and _is_cjk(right[0])
        )
    ):
        return left + right
    return f"{left} {right}"


def _words_text(words: list[Word]) -> str:
    text = ""
    for word in words:
        text = _join_text(text, word.text)
    return text.strip()


@dataclass
class _SpeakerRun:
    segment: Segment
    mergeable: bool = True


def _speaker_runs(segment: Segment) -> tuple[list[_SpeakerRun], bool]:
    """Split a mixed-speaker ASR segment at word-level speaker boundaries."""
    if not segment.words:
        return [_SpeakerRun(segment)], False
    speakers = [word.speaker or segment.speaker for word in segment.words]
    if len(set(speakers)) <= 1:
        return (
            [
                _SpeakerRun(
                    Segment(
                        text=segment.text,
                        start=segment.start,
                        end=segment.end,
                        speaker=speakers[0] if speakers else segment.speaker,
                        words=list(segment.words),
                    )
                )
            ],
            False,
        )

    # If alignment words cannot reproduce the original text, splitting would
    # silently lose or rewrite content. Keep the mixed segment as a standalone
    # paragraph instead of merging it under the majority speaker.
    if _words_text(segment.words) != segment.text.strip():
        return (
            [
                _SpeakerRun(
                    Segment(
                        text=segment.text,
                        start=segment.start,
                        end=segment.end,
                        speaker=None,
                        words=list(segment.words),
                    ),
                    mergeable=False,
                )
            ],
            True,
        )

    runs: list[_SpeakerRun] = []
    start = 0
    for index in range(1, len(segment.words) + 1):
        if index < len(segment.words) and speakers[index] == speakers[start]:
            continue
        words = segment.words[start:index]
        runs.append(
            _SpeakerRun(
                Segment(
                    text=_words_text(words),
                    start=words[0].start,
                    end=words[-1].end,
                    speaker=speakers[start],
                    words=list(words),
                )
            )
        )
        start = index
    return runs, False


def group_speaker_segments(
    transcript: Transcript,
    *,
    max_gap_seconds: float = DEFAULT_SPEAKER_GROUP_GAP_SECONDS,
    max_chars: int = DEFAULT_SPEAKER_GROUP_MAX_CHARS,
    max_duration_seconds: float = DEFAULT_SPEAKER_GROUP_MAX_DURATION_SECONDS,
) -> tuple[Transcript, dict]:
    """Build readable speaker paragraphs without losing word timestamps.

    Word-level speaker changes split an ASR segment first. Consecutive runs are
    then merged only when the speaker is known, unchanged, and the silence gap
    does not exceed ``max_gap_seconds``.
    """
    if max_gap_seconds < 0:
        raise ValueError("speaker grouping gap must be non-negative")
    if max_chars < 1 or max_duration_seconds <= 0:
        raise ValueError("speaker grouping limits must be positive")

    runs: list[_SpeakerRun] = []
    split_boundaries = 0
    unsafe_mixed_segments = 0
    for segment in transcript.segments:
        segment_runs, unsafe = _speaker_runs(segment)
        runs.extend(segment_runs)
        split_boundaries += max(0, len(segment_runs) - 1)
        unsafe_mixed_segments += int(unsafe)

    grouped: list[_SpeakerRun] = []
    merged_boundaries = 0
    limit_boundaries = 0
    for run in runs:
        previous = grouped[-1] if grouped else None
        segment = run.segment
        previous_segment = previous.segment if previous is not None else None
        gap = segment.start - previous_segment.end if previous_segment is not None else None
        candidate_text = (
            _join_text(previous_segment.text, segment.text)
            if previous_segment is not None
            else segment.text
        )
        same_speaker_run = (
            previous is not None
            and previous.mergeable
            and run.mergeable
            and previous_segment is not None
            and previous_segment.speaker is not None
            and previous_segment.speaker == segment.speaker
            and segment.start >= previous_segment.start
            and gap is not None
            and gap <= max_gap_seconds
        )
        within_limits = bool(
            previous_segment is not None
            and len(candidate_text) <= max_chars
            and max(previous_segment.end, segment.end) - previous_segment.start
            <= max_duration_seconds
        )
        if same_speaker_run and within_limits:
            previous_segment.text = candidate_text
            previous_segment.end = max(previous_segment.end, segment.end)
            previous_segment.words.extend(segment.words)
            merged_boundaries += 1
        else:
            if same_speaker_run:
                limit_boundaries += 1
            grouped.append(
                _SpeakerRun(
                    Segment(
                        text=segment.text,
                        start=segment.start,
                        end=segment.end,
                        speaker=segment.speaker,
                        words=list(segment.words),
                    ),
                    mergeable=run.mergeable,
                )
            )

    grouped_segments = [run.segment for run in grouped]
    result = Transcript(
        segments=grouped_segments,
        language=transcript.language,
        duration=transcript.duration,
        provider=transcript.provider,
        model=transcript.model,
        has_speakers=bool(grouped_segments)
        and all(segment.speaker for segment in grouped_segments),
    )
    return result, {
        "strategy": "consecutive-speaker-runs",
        "max_gap_seconds": max_gap_seconds,
        "max_chars": max_chars,
        "max_duration_seconds": max_duration_seconds,
        "input_segments": len(transcript.segments),
        "speaker_runs": len(runs),
        "output_segments": len(grouped),
        "split_boundaries": split_boundaries,
        "merged_boundaries": merged_boundaries,
        "limit_boundaries": limit_boundaries,
        "unsafe_mixed_segments": unsafe_mixed_segments,
    }


class DiarizationError(RuntimeError):
    pass


class DiarizationUnavailable(DiarizationError):
    pass


def _resolve_device(cfg: DiarizeConfig) -> tuple[object, str]:
    try:
        import torch
    except Exception as exc:  # noqa: BLE001 - binary dependency imports can fail broadly
        raise DiarizationUnavailable(f"PyTorch unavailable: {exc}") from exc

    if cfg.device == "cpu":
        return torch, "cpu"

    cuda_available = bool(torch.cuda.is_available())
    if cfg.device == "cuda" and not cuda_available:
        raise DiarizationUnavailable(
            "CUDA requested for diarization but torch.cuda.is_available() is false; "
            "install a CUDA-enabled PyTorch runtime or set diarize.device = \"cpu\""
        )
    resolved = "cuda" if cuda_available else "cpu"
    return torch, resolved


def health(cfg: DiarizeConfig) -> tuple[bool, str]:
    if cfg.provider == "none":
        return False, "disabled; speaker labels will not be generated"
    try:
        import pyannote.audio  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - binary dependency imports can fail broadly
        return False, f"pyannote.audio unavailable: {exc}"
    if not cfg.hf_token:
        return False, "Hugging Face token missing; accept the model terms and set hf_token"
    try:
        _torch, device = _resolve_device(cfg)
    except DiarizationUnavailable as exc:
        return False, str(exc)
    selection = "auto-selected" if cfg.device == "auto" else "configured"
    return True, f"model {cfg.model} configured on {device} ({selection})"


def _load_pipeline(cfg: DiarizeConfig):
    try:
        from pyannote.audio import Pipeline
    except Exception as exc:  # noqa: BLE001
        raise DiarizationUnavailable(f"pyannote.audio not installed: {exc}") from exc
    if not cfg.hf_token:
        raise DiarizationUnavailable(
            "diarize.hf_token not set (needed to download the pyannote pipeline)"
        )
    torch, device = _resolve_device(cfg)
    try:
        pipeline = Pipeline.from_pretrained(
            cfg.model,
            token=cfg.hf_token,
        )
    except Exception as exc:  # noqa: BLE001
        raise DiarizationUnavailable(f"could not load pyannote pipeline: {exc}") from exc
    try:
        pipeline.to(torch.device(device))
    except Exception as exc:  # noqa: BLE001 - device/runtime failures need actionable state
        raise DiarizationUnavailable(
            f"could not move pyannote pipeline to {device}: {exc}"
        ) from exc
    log.info("Loaded pyannote diarization pipeline %s on %s", cfg.model, device)
    return pipeline


def diarize(wav_path, transcript: Transcript, cfg: DiarizeConfig) -> Transcript:
    """Return ``transcript`` with speaker labels filled in. If diarization is
    disabled or unavailable, returns it unchanged."""
    if cfg.provider == "none":
        return transcript
    if transcript.has_speakers:
        return transcript

    pipeline = _load_pipeline(cfg)
    kwargs = {}
    if cfg.num_speakers:
        kwargs["num_speakers"] = cfg.num_speakers
    log.info("Running pyannote diarization on %s", wav_path)
    output = pipeline(str(wav_path), **kwargs)
    annotation = getattr(output, "speaker_diarization", output)

    # Build (start, end, speaker) turns.
    if hasattr(annotation, "itertracks"):
        turns = [
            (turn.start, turn.end, spk)
            for turn, _, spk in annotation.itertracks(yield_label=True)
        ]
    else:
        turns = [(turn.start, turn.end, spk) for turn, spk in annotation]

    if not turns:
        # Diarization found nothing (e.g. near-silent audio) — don't claim we
        # assigned speakers.
        log.info("Diarization produced no turns for %s; leaving speakers unset", wav_path)
        return transcript

    def speaker_for(start: float, end: float) -> str:
        # Zero-length spans (Whisper emits some) become a point query.
        if end <= start:
            for t_start, t_end, spk in turns:
                if t_start <= start <= t_end:
                    return spk
        best, best_overlap = None, 0.0
        for t_start, t_end, spk in turns:
            overlap = max(0.0, min(end, t_end) - max(start, t_start))
            if overlap > best_overlap:
                best, best_overlap = spk, overlap
        if best is not None:
            return best

        # Pyannote speech turns and Whisper timestamps use independent VAD
        # boundaries, so short ASR words/segments can legitimately land in a
        # small gap. Assign the closest detected turn rather than leaving a
        # partially diarized transcript that falsely reports completion. Ties
        # preserve pyannote's deterministic turn order.
        def distance(turn: tuple[float, float, str]) -> float:
            t_start, t_end, _speaker = turn
            if end < t_start:
                return t_start - end
            if start > t_end:
                return start - t_end
            return 0.0

        return min(turns, key=distance)[2]

    for seg in transcript.segments:
        if seg.words:
            for w in seg.words:
                w.speaker = speaker_for(w.start, w.end)
            # Segment speaker = majority of its words.
            counts: dict[str, float] = {}
            for w in seg.words:
                if w.speaker:
                    counts[w.speaker] = counts.get(w.speaker, 0.0) + (w.end - w.start)
            seg.speaker = max(counts, key=counts.get) if counts else speaker_for(seg.start, seg.end)
        else:
            seg.speaker = speaker_for(seg.start, seg.end)

    transcript.has_speakers = bool(transcript.segments) and all(
        segment.speaker for segment in transcript.segments
    )
    return transcript
