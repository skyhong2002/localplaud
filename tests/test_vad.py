"""VAD groundwork: region merging, mlx region-offset ASR, and honest fallback.

None of these touch a real model or real audio — silero-vad is not installed in
CI and mlx-whisper is monkeypatched, matching how the pipeline degrades in the
absence of the optional 'vad' extra.
"""

import logging
import sys
import wave
from types import SimpleNamespace

import pytest

from localplaud.asr import vad
from localplaud.asr.faster_whisper_provider import FasterWhisperProvider
from localplaud.asr.mlx_provider import MlxWhisperProvider
from localplaud.config import AsrConfig, VadConfig

# --------------------------------------------------------------------------- #
# merge_speech_regions
# --------------------------------------------------------------------------- #


def test_merge_empty_input_returns_empty():
    assert vad.merge_speech_regions([], min_gap_s=0.5, pad_s=0.2, max_region_s=30.0) == []


def test_merge_drops_zero_and_reversed_spans():
    regions = [(1.0, 1.0), (2.0, 1.5)]
    assert vad.merge_speech_regions(regions, 0.5, 0.0, 0.0) == []


def test_merge_sorts_and_closes_small_gaps():
    # Out-of-order input; the 0.3s gap merges, but the 0.7s gap stays separate.
    regions = [(2.5, 3.0), (0.0, 1.0), (1.3, 1.8)]
    merged = vad.merge_speech_regions(regions, min_gap_s=0.5, pad_s=0.0, max_region_s=0.0)
    assert merged == [(0.0, 1.8), (2.5, 3.0)]


def test_merge_keeps_large_gaps_separate():
    regions = [(0.0, 1.0), (5.0, 6.0)]
    merged = vad.merge_speech_regions(regions, min_gap_s=0.5, pad_s=0.0, max_region_s=0.0)
    assert merged == [(0.0, 1.0), (5.0, 6.0)]


def test_merge_collapses_overlaps():
    regions = [(0.0, 2.0), (1.0, 3.0)]
    merged = vad.merge_speech_regions(regions, min_gap_s=0.0, pad_s=0.0, max_region_s=0.0)
    assert merged == [(0.0, 3.0)]


def test_merge_pads_and_clamps_at_zero():
    regions = [(0.1, 1.0)]
    merged = vad.merge_speech_regions(regions, min_gap_s=0.5, pad_s=0.2, max_region_s=0.0)
    # start clamps to 0.0, end grows by pad.
    assert merged[0][0] == 0.0
    assert merged[0][1] == pytest.approx(1.2)


def test_merge_padding_causes_remerge():
    # 0.3s gap survives min_gap=0.0 but 0.2s padding on both sides overlaps them.
    regions = [(0.0, 1.0), (1.3, 2.0)]
    merged = vad.merge_speech_regions(regions, min_gap_s=0.0, pad_s=0.2, max_region_s=0.0)
    assert len(merged) == 1
    assert merged[0][0] == 0.0
    assert merged[0][1] == pytest.approx(2.2)


def test_merge_splits_long_regions():
    regions = [(0.0, 70.0)]
    merged = vad.merge_speech_regions(regions, min_gap_s=0.5, pad_s=0.0, max_region_s=30.0)
    assert merged == [(0.0, 30.0), (30.0, 60.0), (60.0, 70.0)]


def test_merge_zero_max_region_disables_splitting():
    regions = [(0.0, 100.0)]
    merged = vad.merge_speech_regions(regions, min_gap_s=0.5, pad_s=0.0, max_region_s=0.0)
    assert merged == [(0.0, 100.0)]


# --------------------------------------------------------------------------- #
# health()
# --------------------------------------------------------------------------- #


def test_health_disabled():
    ok, detail = vad.health(VadConfig(enabled=False))
    assert ok is False
    assert "disabled" in detail


def test_health_enabled_but_missing_package(monkeypatch):
    # Ensure silero_vad import fails regardless of the host environment.
    monkeypatch.setitem(sys.modules, "silero_vad", None)
    ok, detail = vad.health(VadConfig(enabled=True))
    assert ok is False
    assert "silero-vad" in detail
    assert "vad" in detail  # names the optional extra


def test_detect_speech_raises_unavailable_without_package(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "silero_vad", None)
    with pytest.raises(vad.VadUnavailable) as exc:
        vad.detect_speech(tmp_path / "x.wav", VadConfig(enabled=True))
    assert "silero-vad" in str(exc.value)


def test_audio_duration_seconds_reads_canonical_pcm_wav(tmp_path):
    path = tmp_path / "two-seconds.wav"
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(vad.SAMPLE_RATE)
        wav_file.writeframes(b"\0\0" * vad.SAMPLE_RATE * 2)

    assert vad.audio_duration_seconds(path) == 2.0


# --------------------------------------------------------------------------- #
# mlx region-offset transcription (no real audio / model)
# --------------------------------------------------------------------------- #


def _region_local_result(text: str):
    """A fake mlx-whisper result with region-LOCAL timestamps (start at 0)."""
    return {
        "language": "zh",
        "segments": [
            {
                "text": text,
                "start": 0.0,
                "end": 1.0,
                "words": [{"word": text, "start": 0.0, "end": 1.0, "probability": 0.9}],
            }
        ],
    }


def _mlx_cfg(**vad_kwargs) -> AsrConfig:
    return AsrConfig(vad=VadConfig(enabled=True, **vad_kwargs))


def test_mlx_offsets_regions_into_global_timeline(monkeypatch):
    # Two well-separated regions; disable extra padding/merging so they pass
    # through unchanged and the offset math is easy to assert.
    monkeypatch.setattr(vad, "detect_speech", lambda path, cfg: [(0.0, 5.0), (10.0, 15.0)])
    monkeypatch.setattr(vad, "slice_region", lambda src, s, e, dst: dst)

    calls = []

    def fake_transcribe(path, **kwargs):
        calls.append((path, kwargs))
        return _region_local_result("李宗盛")

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))

    cfg = _mlx_cfg(region_pad_s=0.0, merge_gap_s=0.0, max_region_s=1000.0)
    provider = MlxWhisperProvider(cfg)
    transcript = provider._transcribe_regions(
        sys.modules["mlx_whisper"], "audio.wav", [(0.0, 5.0), (10.0, 15.0)], "auto"
    )

    assert len(transcript.segments) == 2
    # Region-local 0.0/1.0 shifted by each region start.
    assert transcript.segments[0].start == 0.0
    assert transcript.segments[0].end == 1.0
    assert transcript.segments[1].start == 10.0
    assert transcript.segments[1].end == 11.0
    # Words offset too.
    assert transcript.segments[1].words[0].start == 10.0
    assert transcript.segments[1].words[0].end == 11.0
    assert transcript.text == "李宗盛\n李宗盛"
    assert transcript.language == "zh"
    assert transcript.provider == "mlx-whisper"
    assert len(calls) == 2
    for _path, kwargs in calls:
        assert kwargs["word_timestamps"] is True
        assert kwargs["condition_on_previous_text"] is False
        assert kwargs["hallucination_silence_threshold"] == 2.0


def test_mlx_vad_enabled_end_to_end_uses_regions(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(vad, "detect_speech", lambda path, cfg: [(2.0, 4.0)])
    monkeypatch.setattr(vad, "slice_region", lambda src, s, e, dst: dst)
    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        SimpleNamespace(transcribe=lambda path, **kw: _region_local_result("hi")),
    )

    provider = MlxWhisperProvider(_mlx_cfg(region_pad_s=0.0, merge_gap_s=0.0))
    transcript = provider.transcribe("audio.wav", language="auto")
    # Region started at 2.0, local 0.0/1.0 -> global 2.0/3.0.
    assert [round(s.start, 3) for s in transcript.segments] == [2.0]
    assert [round(s.end, 3) for s in transcript.segments] == [3.0]


def test_mlx_vad_enabled_but_unavailable_falls_back_and_logs(monkeypatch, caplog):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")

    def raise_unavailable(path, cfg):
        raise vad.VadUnavailable("silero-vad is not installed")

    monkeypatch.setattr(vad, "detect_speech", raise_unavailable)
    whole_file_calls = []

    def fake_transcribe(path, **kwargs):
        whole_file_calls.append(path)
        # whole-file result with already-global timestamps.
        return {
            "language": "en",
            "segments": [
                {"text": "hello", "start": 0.0, "end": 2.0, "words": []},
            ],
        }

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))

    provider = MlxWhisperProvider(_mlx_cfg())
    with caplog.at_level(logging.WARNING):
        transcript = provider.transcribe("audio.wav", language="auto")

    # Transcript is still produced (subscription independence must not break).
    assert transcript.text == "hello"
    assert whole_file_calls == ["audio.wav"]
    assert any("unavailable" in r.message.lower() for r in caplog.records)


def test_mlx_vad_no_speech_returns_empty_without_calling_asr(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(vad, "detect_speech", lambda path, cfg: [])  # no speech found

    def unexpected_transcribe(*args, **kwargs):
        raise AssertionError("Whisper must not run after VAD successfully finds no speech")

    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        SimpleNamespace(transcribe=unexpected_transcribe),
    )

    provider = MlxWhisperProvider(_mlx_cfg())
    transcript = provider.transcribe("audio.wav", language="auto")

    assert transcript.segments == []
    assert transcript.text == ""
    assert transcript.language is None
    assert transcript.duration is None
    assert transcript.provider == "mlx-whisper"
    assert transcript.model == provider.cfg.model


def test_mlx_whole_file_passes_hallucination_decoder_kwargs(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
    calls = []

    def fake_transcribe(path, **kwargs):
        calls.append((path, kwargs))
        return _region_local_result("李宗盛")

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=fake_transcribe))

    transcript = MlxWhisperProvider(AsrConfig()).transcribe("audio.wav", language="zh")

    assert transcript.text == "李宗盛"
    assert calls == [
        (
            "audio.wav",
            {
                "path_or_hf_repo": MlxWhisperProvider(AsrConfig()).cfg.model,
                "word_timestamps": True,
                "condition_on_previous_text": False,
                "hallucination_silence_threshold": 2.0,
                "language": "zh",
            },
        )
    ]


def test_mlx_vad_disabled_is_unchanged(monkeypatch):
    # Guard: with VAD off, detect_speech/slice_region must never be touched.
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")

    def boom(*a, **k):
        raise AssertionError("VAD path must not run when disabled")

    monkeypatch.setattr(vad, "detect_speech", boom)
    monkeypatch.setattr(vad, "slice_region", boom)
    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        SimpleNamespace(
            transcribe=lambda path, **kw: {
                "language": "en",
                "segments": [{"text": "plain", "start": 1.5, "end": 2.5, "words": []}],
            }
        ),
    )

    provider = MlxWhisperProvider(AsrConfig())  # vad disabled by default
    transcript = provider.transcribe("audio.wav", language="auto")
    assert transcript.text == "plain"
    assert transcript.segments[0].start == 1.5


# --------------------------------------------------------------------------- #
# provider health() exposes VAD state
# --------------------------------------------------------------------------- #


def test_mlx_health_reports_vad_degraded(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "silero_vad", None)  # force VAD unavailable

    provider = MlxWhisperProvider(_mlx_cfg())
    ok, detail = provider.health()
    assert ok is True  # ASR itself is fine; VAD is only degraded
    assert "VAD enabled but degraded" in detail
    assert "whole-file" in detail


def test_mlx_health_no_vad_mention_when_disabled(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace())

    provider = MlxWhisperProvider(AsrConfig())
    ok, detail = provider.health()
    assert ok is True
    assert "VAD" not in detail


# --------------------------------------------------------------------------- #
# faster-whisper shared acoustic pre-gate and native VAD fallback
# --------------------------------------------------------------------------- #


def test_faster_whisper_shared_vad_empty_returns_without_loading_model(monkeypatch):
    monkeypatch.setattr(vad, "detect_speech", lambda path, cfg: [])
    monkeypatch.setattr(vad, "audio_duration_seconds", lambda path: 76.0)

    class UnexpectedModel:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Whisper model must not load when shared VAD finds no speech")

    monkeypatch.setitem(
        sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=UnexpectedModel)
    )

    transcript = FasterWhisperProvider(_mlx_cfg()).transcribe("audio.wav", language="auto")

    assert transcript.segments == []
    assert transcript.text == ""
    assert transcript.language is None
    assert transcript.duration == 76.0
    assert transcript.provider == "faster-whisper"
    assert transcript.model == FasterWhisperProvider(_mlx_cfg()).cfg.model


def test_faster_whisper_shared_vad_passes_global_clips_and_preserves_output(monkeypatch):
    calls = []

    monkeypatch.setattr(
        vad, "detect_speech", lambda path, cfg: [(15.7, 16.5), (17.3, 17.9), (75.8, 75.95)]
    )
    monkeypatch.setattr(vad, "audio_duration_seconds", lambda path: 76.0)

    class FakeModel:
        def __init__(self, model, *, device, compute_type):
            calls.append(("init", model, device, compute_type))

        def transcribe(self, path, **kwargs):
            calls.append(("transcribe", path, kwargs))
            word = SimpleNamespace(word=" 李宗盛", start=15.7, end=16.5, probability=0.98)
            segment = SimpleNamespace(text=" 李宗盛", start=15.7, end=16.5, words=[word])
            return iter([segment]), SimpleNamespace(language="zh", duration=76.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    cfg = AsrConfig(
        vad=VadConfig(
            enabled=True,
            threshold=0.61,
            min_speech_ms=321,
            min_silence_ms=654,
            speech_pad_ms=87,
            region_pad_s=0.2,
            merge_gap_s=0.0,
            max_region_s=45.0,
        ),
        faster_whisper={"device": "cpu", "compute_type": "int8"},
    )

    transcript = FasterWhisperProvider(cfg).transcribe("audio.wav", language="auto")

    assert transcript.text == "李宗盛"
    assert transcript.segments[0].start == 15.7
    assert transcript.segments[0].words[0].start == 15.7
    assert transcript.duration == 76.0
    _, path, kwargs = calls[1]
    assert path == "audio.wav"
    assert kwargs == {
        "language": None,
        "word_timestamps": True,
        "condition_on_previous_text": False,
        "hallucination_silence_threshold": 2.0,
        "clip_timestamps": pytest.approx([15.5, 16.7, 17.1, 18.1, 75.6, 76.0]),
    }


def test_faster_whisper_missing_shared_vad_uses_native_vad_fallback(monkeypatch, caplog):
    calls = []

    def unavailable(path, cfg):
        raise vad.VadUnavailable("silero-vad is not installed")

    monkeypatch.setattr(vad, "detect_speech", unavailable)
    monkeypatch.setattr(vad, "audio_duration_seconds", lambda path: 76.0)

    class FakeModel:
        def __init__(self, model, *, device, compute_type):
            calls.append(("init", model, device, compute_type))

        def transcribe(self, path, **kwargs):
            calls.append(("transcribe", path, kwargs))
            return iter([]), SimpleNamespace(language="zh", duration=76.0)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeModel))
    cfg = AsrConfig(
        vad=VadConfig(
            enabled=True,
            threshold=0.61,
            min_speech_ms=321,
            min_silence_ms=654,
            speech_pad_ms=87,
            max_region_s=45.0,
        ),
        faster_whisper={"device": "cpu", "compute_type": "int8"},
    )

    with caplog.at_level(logging.WARNING):
        transcript = FasterWhisperProvider(cfg).transcribe("audio.wav", language="auto")

    assert transcript.duration == 76.0
    _, _, kwargs = calls[1]
    assert "clip_timestamps" not in kwargs
    assert kwargs["vad_filter"] is True
    assert kwargs["vad_parameters"] == {
        "threshold": 0.61,
        "min_speech_duration_ms": 321,
        "min_silence_duration_ms": 654,
        "speech_pad_ms": 87,
        "max_speech_duration_s": 45.0,
    }
    assert any("native VAD filter" in record.message for record in caplog.records)
