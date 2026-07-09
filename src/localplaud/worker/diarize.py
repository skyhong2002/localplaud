"""Speaker diarization — assign speaker labels to a transcript.

Only runs when the ASR provider didn't already return speakers. Uses
pyannote.audio locally (needs a HuggingFace token to fetch the pipeline). The
diarization timeline is intersected with each word/segment: a segment gets the
speaker who overlaps it most.
"""

from __future__ import annotations

import logging

from ..asr.base import Transcript
from ..config import DiarizeConfig

log = logging.getLogger(__name__)


class DiarizationError(RuntimeError):
    pass


class DiarizationUnavailable(DiarizationError):
    pass


def _load_pipeline(cfg: DiarizeConfig):
    try:
        from pyannote.audio import Pipeline
    except Exception as exc:  # noqa: BLE001
        raise DiarizationUnavailable(f"pyannote.audio not installed: {exc}") from exc
    if not cfg.hf_token:
        raise DiarizationUnavailable(
            "diarize.hf_token not set (needed to download the pyannote pipeline)"
        )
    try:
        return Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1", use_auth_token=cfg.hf_token
        )
    except Exception as exc:  # noqa: BLE001
        raise DiarizationUnavailable(f"could not load pyannote pipeline: {exc}") from exc


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
    annotation = pipeline(str(wav_path), **kwargs)

    # Build (start, end, speaker) turns.
    turns = [(turn.start, turn.end, spk) for turn, _, spk in annotation.itertracks(yield_label=True)]

    if not turns:
        # Diarization found nothing (e.g. near-silent audio) — don't claim we
        # assigned speakers.
        log.info("Diarization produced no turns for %s; leaving speakers unset", wav_path)
        return transcript

    def speaker_for(start: float, end: float) -> str | None:
        # Zero-length spans (Whisper emits some) become a point query.
        if end <= start:
            for t_start, t_end, spk in turns:
                if t_start <= start <= t_end:
                    return spk
            return None
        best, best_overlap = None, 0.0
        for t_start, t_end, spk in turns:
            overlap = max(0.0, min(end, t_end) - max(start, t_start))
            if overlap > best_overlap:
                best, best_overlap = spk, overlap
        return best

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

    transcript.has_speakers = True
    return transcript
