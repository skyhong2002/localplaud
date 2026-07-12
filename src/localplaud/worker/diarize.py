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
