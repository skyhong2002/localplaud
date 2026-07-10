"""ASR via mlx-whisper — local, Apple Silicon (MLX) only."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from . import vad
from .base import AsrError, AsrUnavailable, Segment, Transcript, Word
from .registry import register

log = logging.getLogger(__name__)


def _shift(value: float, offset: float) -> float:
    # Preserve byte-identical whole-file output: a falsy (0.0) offset is a no-op.
    return value + offset if offset else value


def _build_segments(raw_segments, offset: float = 0.0) -> list[Segment]:
    """Convert mlx-whisper's raw segment dicts into our data model.

    ``offset`` shifts every timestamp so per-region (VAD-chunked) transcription
    still yields global timestamps. With the default 0.0 offset the output is
    identical to whole-file transcription.
    """
    return [
        Segment(
            text=seg.get("text", ""),
            start=_shift(seg.get("start", 0.0), offset),
            end=_shift(seg.get("end", 0.0), offset),
            words=[
                Word(
                    text=w.get("word", ""),
                    start=_shift(w.get("start", 0.0), offset),
                    end=_shift(w.get("end", 0.0), offset),
                    confidence=w.get("probability"),
                )
                for w in seg.get("words", [])
            ],
        )
        for seg in raw_segments
    ]


class MlxWhisperProvider:
    name = "mlx-whisper"

    def __init__(self, cfg):
        self.cfg = cfg.mlx_whisper
        self.vad = cfg.vad
        self.language = cfg.language

    def available(self) -> bool:
        return self.health()[0]

    def health(self) -> tuple[bool, str]:
        import shutil

        try:
            import mlx_whisper  # noqa: F401
        except ImportError as exc:
            return False, f"mlx-whisper import failed: {exc}"
        # mlx-whisper shells out to ffmpeg in load_audio; a bare daemon/launchd
        # PATH often lacks it, so treat a missing ffmpeg as unavailable.
        if shutil.which("ffmpeg") is None:
            return False, "ffmpeg missing from PATH"
        detail = f"model {self.cfg.model} configured"
        # Surface the VAD situation honestly: enabled-but-unavailable is a
        # degraded (not failed) state — ASR still runs on the whole file.
        if self.vad.enabled:
            vad_ok, vad_detail = vad.health(self.vad)
            detail += (
                "; VAD enabled (silero-vad)"
                if vad_ok
                else f"; VAD enabled but degraded, ASR falls back to whole-file: {vad_detail}"
            )
        return True, detail

    def transcribe(self, audio_path: Path, language: str = "auto") -> Transcript:
        import shutil

        try:
            import mlx_whisper
        except ImportError as exc:
            raise AsrUnavailable("mlx-whisper is not installed") from exc
        if shutil.which("ffmpeg") is None:
            raise AsrUnavailable("mlx-whisper needs ffmpeg on PATH")

        if self.vad.enabled:
            try:
                regions = vad.detect_speech(audio_path, self.vad)
            except vad.VadUnavailable as exc:
                log.warning(
                    "VAD is enabled but unavailable (%s); falling back to whole-file "
                    "transcription of %s",
                    exc,
                    audio_path,
                )
            else:
                merged = vad.merge_speech_regions(
                    regions,
                    self.vad.merge_gap_s,
                    self.vad.region_pad_s,
                    self.vad.max_region_s,
                )
                if merged:
                    return self._transcribe_regions(mlx_whisper, audio_path, merged, language)
                log.warning(
                    "VAD found no speech regions in %s; falling back to whole-file "
                    "transcription",
                    audio_path,
                )

        log.info("Transcribing with mlx-whisper model %s", self.cfg.model)
        result = self._run(mlx_whisper, audio_path, language)
        segments = _build_segments(result.get("segments", []))
        return Transcript(
            segments=segments,
            language=result.get("language"),
            duration=segments[-1].end if segments else None,
            provider=self.name,
            model=self.cfg.model,
            has_speakers=False,
        )

    def _run(self, mlx_whisper, path, language: str) -> dict:
        try:
            return mlx_whisper.transcribe(
                str(path),
                path_or_hf_repo=self.cfg.model,
                word_timestamps=True,
                language=None if language == "auto" else language,
            )
        except Exception as exc:
            raise AsrError(f"mlx-whisper transcription failed: {exc}") from exc

    def _transcribe_regions(
        self, mlx_whisper, audio_path: Path, regions: list[tuple[float, float]], language: str
    ) -> Transcript:
        log.info(
            "Transcribing %d VAD speech region(s) with mlx-whisper model %s",
            len(regions),
            self.cfg.model,
        )
        all_segments: list[Segment] = []
        detected_language: str | None = None
        with tempfile.TemporaryDirectory(prefix="localplaud-vad-") as tmp:
            for idx, (start, end) in enumerate(regions):
                clip = Path(tmp) / f"region_{idx:05d}.wav"
                vad.slice_region(audio_path, start, end, clip)
                try:
                    result = mlx_whisper.transcribe(
                        str(clip),
                        path_or_hf_repo=self.cfg.model,
                        word_timestamps=True,
                        language=None if language == "auto" else language,
                    )
                except Exception as exc:
                    raise AsrError(
                        f"mlx-whisper transcription failed on region {idx} "
                        f"[{start:.2f}-{end:.2f}s]: {exc}"
                    ) from exc
                if detected_language is None:
                    detected_language = result.get("language")
                # Offset region-local timestamps back to global time.
                all_segments.extend(_build_segments(result.get("segments", []), offset=start))
        return Transcript(
            segments=all_segments,
            language=detected_language,
            duration=all_segments[-1].end if all_segments else None,
            provider=self.name,
            model=self.cfg.model,
            has_speakers=False,
        )


@register("mlx-whisper")
def _factory(cfg):
    return MlxWhisperProvider(cfg)
