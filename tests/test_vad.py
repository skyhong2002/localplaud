"""VAD groundwork: region merging, mlx region-offset ASR, and honest fallback.

None of these touch a real model or real audio — silero-vad is not installed in
CI and mlx-whisper is monkeypatched, matching how the pipeline degrades in the
absence of the optional 'vad' extra.
"""

import logging
import sys
from types import SimpleNamespace

import pytest

from localplaud.asr import vad
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
        calls.append(path)
        return _region_local_result("你好")

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
    assert transcript.language == "zh"
    assert transcript.provider == "mlx-whisper"
    assert len(calls) == 2


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


def test_mlx_vad_no_speech_falls_back_to_whole_file(monkeypatch, caplog):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(vad, "detect_speech", lambda path, cfg: [])  # no speech found
    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        SimpleNamespace(
            transcribe=lambda path, **kw: {
                "language": "en",
                "segments": [{"text": "whole", "start": 0.0, "end": 1.0, "words": []}],
            }
        ),
    )

    provider = MlxWhisperProvider(_mlx_cfg())
    with caplog.at_level(logging.WARNING):
        transcript = provider.transcribe("audio.wav", language="auto")
    assert transcript.text == "whole"
    assert any("no speech" in r.message.lower() for r in caplog.records)


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
