"""Speaker diarization — assign speaker labels to a transcript.

Only runs when the ASR provider didn't already return speakers. Uses
pyannote.audio locally (needs a HuggingFace token to fetch the pipeline). The
diarization timeline is intersected with each word/segment: a segment gets the
speaker who overlaps it most.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..asr.base import Segment, Transcript, Word
from ..config import DiarizeConfig

log = logging.getLogger(__name__)
DEFAULT_SPEAKER_GROUP_GAP_SECONDS = 3.0
DEFAULT_SPEAKER_GROUP_MAX_CHARS = 1_200
DEFAULT_SPEAKER_GROUP_MAX_DURATION_SECONDS = 120.0
DEFAULT_SPEAKER_TURN_PAUSE_SECONDS = 1.0
_MIN_PAUSE_CLAUSE_CHARS = 2
SPEAKER_TURN_STRATEGY = "clause-majority/v1"
_CLAUSE_MARKS = frozenset("，。！？；：、…,.!?;:")
_OPENING_MARKS = frozenset("([{「『【《（“‘")


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


def _dominant_speaker(segment: Segment) -> str | None:
    """Choose a deterministic display speaker without changing word ownership."""
    weights: dict[str, float] = {}
    for word in segment.words:
        speaker = word.speaker or segment.speaker
        if speaker:
            weights[speaker] = weights.get(speaker, 0.0) + max(0.0, word.end - word.start)
    return max(weights, key=weights.get) if weights else segment.speaker


@dataclass
class _SpeakerRun:
    segment: Segment
    mergeable: bool = True


def _fold(char: str) -> str:
    return unicodedata.normalize("NFKC", char).casefold()


def _word_spans(text: str, words: list[Word]) -> list[tuple[int, int]] | None:
    """Locate each word's letters and digits, in order, inside ``text``.

    Forced aligners drop the punctuation and spacing that ASR text keeps, so
    words rarely concatenate back to the segment text. Matching only lexical
    characters still proves each word owns its own span and that no text is
    unaccounted for. Returns ``None`` when the words cannot reproduce the text.
    """
    position = 0
    spans: list[tuple[int, int]] = []
    for word in words:
        key = "".join(_fold(char) for char in word.text if char.isalnum())
        start = None
        matched = ""
        while matched != key:
            if position >= len(text):
                return None
            char = text[position]
            if char.isalnum():
                matched += _fold(char)
                if not key.startswith(matched):
                    return None
                if start is None:
                    start = position
            position += 1
        spans.append((position, position) if start is None else (start, position))
    if any(char.isalnum() for char in text[position:]):
        return None
    return spans


def _cut_position(text: str, left: tuple[int, int], right: tuple[int, int]) -> int | None:
    """Where text between two words divides, or ``None`` inside a Latin token.

    Trailing punctuation and spacing stay with the earlier word; an opening
    bracket or quote moves with the later one.
    """
    gap = range(left[1], right[0])
    if not gap and not (
        left[1] > left[0] and _is_cjk(text[left[1] - 1])
        or right[1] > right[0] and _is_cjk(text[right[0]])
    ):
        return None
    return next((index for index in gap if text[index] in _OPENING_MARKS), right[0])


def _lexical_run(text: str, offset: int, step: int) -> int:
    """Letters and digits from ``offset`` to the nearest clause mark."""
    count = 0
    index = offset if step > 0 else offset - 1
    while 0 <= index < len(text) and text[index] not in _CLAUSE_MARKS:
        count += text[index].isalnum()
        index += step
    return count


def _clause_speaker(words: list[Word], fallback: str | None) -> str | None:
    weights: dict[str, float] = {}
    for word in words:
        speaker = word.speaker or fallback
        if speaker:
            # A small per-word weight keeps zero-length timestamps meaningful.
            weights[speaker] = weights.get(speaker, 0.0) + max(0.0, word.end - word.start) + 0.01
    return max(weights, key=weights.get) if weights else fallback


def _clause_turns(
    text: str, words: list[Word], fallback: str | None, pause_seconds: float
) -> list[tuple[int, int, str | None]] | None:
    """Speaker turns as ``(first word index, text offset, speaker)``.

    Diarization boundaries and word timestamps come from independent models,
    so individual words near a turn change often flicker between speakers.
    Speakers are therefore chosen per clause (punctuation or a long pause), by
    the speaker who holds most of that clause's speech.
    """
    spans = _word_spans(text, words)
    if spans is None:
        return None
    clauses = [(0, 0)]
    for index in range(1, len(words)):
        cut = _cut_position(text, spans[index - 1], spans[index])
        if cut is None:
            continue
        gap = text[spans[index - 1][1] : spans[index][0]]
        if any(char in _CLAUSE_MARKS for char in gap) or (
            words[index].start - words[index - 1].end >= pause_seconds
            # A pause must not strand a fragment of an unpunctuated phrase.
            and _lexical_run(text, cut, -1) >= _MIN_PAUSE_CLAUSE_CHARS
            and _lexical_run(text, cut, 1) >= _MIN_PAUSE_CLAUSE_CHARS
        ):
            clauses.append((index, cut))
    turns: list[tuple[int, int, str | None]] = []
    for (first, offset), (last, _offset) in zip(
        clauses, [*clauses[1:], (len(words), len(text))], strict=True
    ):
        speaker = _clause_speaker(words[first:last], fallback)
        if not turns or turns[-1][2] != speaker:
            turns.append((first, offset, speaker))
    return turns


def _project_offsets(source: str, target: str, offsets: list[int]) -> list[int] | None:
    """Carry cut offsets from ``source`` into an edited copy of it.

    Corrected text keeps the aligned words' timing but not their exact
    spelling, so cuts move through a character diff. A cut inside a rewritten
    span snaps to its nearer edge; trailing punctuation stays with the
    earlier turn. Returns ``None`` if any projected turn would lose all text.
    """
    from difflib import SequenceMatcher

    blocks = SequenceMatcher(None, source, target, autojunk=False).get_opcodes()
    projected = []
    for offset in offsets:
        _tag, i1, i2, j1, j2 = next(block for block in blocks if block[1] <= offset <= block[2])
        if i2 - i1 == j2 - j1:
            position = j1 + offset - i1
        else:
            position = j1 if offset - i1 <= i2 - offset else j2
        while offset and position < len(target) and (
            target[position] in _CLAUSE_MARKS or target[position].isspace()
        ):
            position += 1
        projected.append(position)
    bounds = [*projected, len(target)]
    if any(
        right <= left or not any(char.isalnum() for char in target[left:right])
        for left, right in zip(bounds, bounds[1:], strict=False)
    ):
        return None
    return projected


def _speaker_runs(
    segment: Segment,
    *,
    source_text: str | None = None,
    pause_seconds: float = DEFAULT_SPEAKER_TURN_PAUSE_SECONDS,
) -> tuple[list[_SpeakerRun], bool]:
    """Split a mixed-speaker ASR segment into clause-level speaker turns.

    ``source_text`` is the text the words were aligned against, for a segment
    whose own text has since been corrected.
    """
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

    words = segment.words
    turns = _clause_turns(segment.text, words, segment.speaker, pause_seconds)
    if turns is None and source_text:
        source_turns = _clause_turns(source_text, words, segment.speaker, pause_seconds)
        offsets = (
            _project_offsets(source_text, segment.text, [offset for _, offset, _ in source_turns])
            if source_turns is not None
            else None
        )
        if offsets is not None:
            turns = [
                (first, offset, speaker)
                for (first, _, speaker), offset in zip(source_turns, offsets, strict=True)
            ]
    # If alignment words cannot reproduce the original text, splitting would
    # silently lose or rewrite content. Keep the mixed segment as a standalone
    # paragraph instead of merging it under the majority speaker.
    if turns is None:
        return (
            [
                _SpeakerRun(
                    Segment(
                        text=segment.text,
                        start=segment.start,
                        end=segment.end,
                        speaker=_dominant_speaker(segment),
                        words=list(words),
                    ),
                    mergeable=False,
                )
            ],
            True,
        )

    runs = []
    bounds = [*turns, (len(words), len(segment.text), None)]
    for (first, offset, speaker), (last, end, _speaker) in zip(bounds, bounds[1:], strict=False):
        owned = words[first:last]
        runs.append(
            _SpeakerRun(
                Segment(
                    text=segment.text[offset:end].strip(),
                    start=owned[0].start,
                    end=owned[-1].end,
                    speaker=speaker,
                    words=list(owned),
                )
            )
        )
    return runs, False


def group_speaker_segments(
    transcript: Transcript,
    *,
    max_gap_seconds: float = DEFAULT_SPEAKER_GROUP_GAP_SECONDS,
    max_chars: int = DEFAULT_SPEAKER_GROUP_MAX_CHARS,
    max_duration_seconds: float = DEFAULT_SPEAKER_GROUP_MAX_DURATION_SECONDS,
    source_texts: list[str | None] | None = None,
) -> tuple[Transcript, dict]:
    """Build readable speaker paragraphs without losing word timestamps.

    Clause-level speaker changes split an ASR segment first. Consecutive runs
    are then merged only when the speaker is known, unchanged, and the silence
    gap does not exceed ``max_gap_seconds``. ``source_texts`` optionally gives,
    per segment, the text its words were aligned against before correction.
    """
    if max_gap_seconds < 0:
        raise ValueError("speaker grouping gap must be non-negative")
    if max_chars < 1 or max_duration_seconds <= 0:
        raise ValueError("speaker grouping limits must be positive")

    runs: list[_SpeakerRun] = []
    split_boundaries = 0
    unsafe_mixed_segments = 0
    for index, segment in enumerate(transcript.segments):
        segment_runs, unsafe = _speaker_runs(
            segment, source_text=source_texts[index] if source_texts else None
        )
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
        "turn_strategy": SPEAKER_TURN_STRATEGY,
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


def _cached_pipeline_config(model: str) -> Path | None:
    """Return a complete cached pipeline config without requiring live HF auth.

    Community-1 vendors its segmentation, embedding, and PLDA artifacts beneath
    the same snapshot. Once the gated model has been accepted and downloaded,
    production should remain usable offline instead of requiring the original
    Hugging Face token on every daemon restart.
    """
    try:
        from huggingface_hub import try_to_load_from_cache
    except Exception:  # noqa: BLE001 - optional dependency/version mismatch
        return None
    try:
        cached = try_to_load_from_cache(model, "config.yaml")
    except Exception:  # noqa: BLE001 - a corrupt cache must degrade cleanly
        return None
    if isinstance(cached, str):
        path = Path(cached)
        # community-1 is a self-contained bundle. Merely finding config.yaml is
        # not sufficient: an interrupted gated download can leave that file
        # while one of the weights or PLDA artifacts is absent.
        required = (
            "segmentation/pytorch_model.bin",
            "embedding/pytorch_model.bin",
            "plda/xvec_transform.npz",
            "plda/plda.npz",
        )
        if path.is_file() and all((path.parent / item).is_file() for item in required):
            return path
    return None


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
    if cfg.provider == "nemotron":
        import shutil
        import sys

        available = bool(shutil.which(cfg.python or sys.executable))
        return available, "Nemotron isolated CUDA runtime configured; maximum eight speakers"
    try:
        import pyannote.audio  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - binary dependency imports can fail broadly
        return False, f"pyannote.audio unavailable: {exc}"
    cached = _cached_pipeline_config(cfg.model)
    if not cfg.hf_token and cached is None:
        return False, "Hugging Face token missing; accept the model terms and set hf_token"
    try:
        _torch, device = _resolve_device(cfg)
    except DiarizationUnavailable as exc:
        return False, str(exc)
    selection = "auto-selected" if cfg.device == "auto" else "configured"
    source = "cached offline" if not cfg.hf_token else "authenticated"
    return True, f"model {cfg.model} configured on {device} ({selection}); {source}"


def _load_pipeline(cfg: DiarizeConfig):
    try:
        from pyannote.audio import Pipeline
    except Exception as exc:  # noqa: BLE001
        raise DiarizationUnavailable(f"pyannote.audio not installed: {exc}") from exc
    cached = _cached_pipeline_config(cfg.model)
    if not cfg.hf_token and cached is None:
        raise DiarizationUnavailable(
            "diarize.hf_token not set and the pyannote pipeline is not cached locally"
        )
    torch, device = _resolve_device(cfg)
    try:
        pipeline = Pipeline.from_pretrained(
            cached if cached is not None and not cfg.hf_token else cfg.model,
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

    if cfg.provider == "nemotron":
        from ..asr.qwen_provider import run_speech_process

        turns = run_speech_process(
            "nemotron", Path(wav_path), cfg.model_dump(), cfg.python, cfg.timeout_seconds
        )["turns"]
    else:
        pipeline = _load_pipeline(cfg)
        kwargs = {}
        if cfg.num_speakers:
            kwargs["num_speakers"] = cfg.num_speakers
        else:
            if cfg.min_speakers:
                kwargs["min_speakers"] = cfg.min_speakers
            if cfg.max_speakers:
                kwargs["max_speakers"] = cfg.max_speakers
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
