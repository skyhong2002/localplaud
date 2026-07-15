"""Speaker assignment and device selection for pyannote Community-1."""

import sys
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import ValidationError

from localplaud.asr.base import Segment, Transcript, Word
from localplaud.config import DiarizeConfig
from localplaud.worker import diarize as diarize_module


def _install_fake_runtime(monkeypatch, *, cuda_available: bool, pipeline=None):
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda_available),
        device=lambda name: SimpleNamespace(type=name),
    )
    fake_pyannote = ModuleType("pyannote")
    fake_pyannote.__path__ = []
    fake_audio = ModuleType("pyannote.audio")
    fake_audio.Pipeline = pipeline or SimpleNamespace()
    fake_pyannote.audio = fake_audio
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "pyannote", fake_pyannote)
    monkeypatch.setitem(sys.modules, "pyannote.audio", fake_audio)
    return fake_torch


@pytest.mark.parametrize("device", ["auto", "cpu", "cuda"])
def test_diarize_config_accepts_explicit_supported_devices(device):
    assert DiarizeConfig(device=device).device == device


def test_diarize_config_rejects_unverified_mps_device():
    with pytest.raises(ValidationError):
        DiarizeConfig(device="mps")


def test_health_auto_reports_resolved_cpu(monkeypatch):
    _install_fake_runtime(monkeypatch, cuda_available=False)

    ok, detail = diarize_module.health(DiarizeConfig(hf_token="test", device="auto"))

    assert ok is True
    assert "on cpu (auto-selected)" in detail


def test_health_explicit_unavailable_cuda_is_actionable(monkeypatch):
    _install_fake_runtime(monkeypatch, cuda_available=False)

    ok, detail = diarize_module.health(DiarizeConfig(hf_token="test", device="cuda"))

    assert ok is False
    assert "torch.cuda.is_available() is false" in detail
    assert 'diarize.device = "cpu"' in detail


@pytest.mark.parametrize(
    ("configured_device", "cuda_available", "expected_device"),
    [("auto", True, "cuda"), ("auto", False, "cpu"), ("cpu", True, "cpu")],
)
def test_load_pipeline_moves_it_to_resolved_device(
    monkeypatch, configured_device, cuda_available, expected_device
):
    loaded = SimpleNamespace(moved_to=None)

    def move_to(device):
        loaded.moved_to = device.type

    loaded.to = move_to

    class FakePipeline:
        @staticmethod
        def from_pretrained(model, token):
            assert model == "test-model"
            assert token == "test-token"
            return loaded

    _install_fake_runtime(
        monkeypatch, cuda_available=cuda_available, pipeline=FakePipeline
    )

    result = diarize_module._load_pipeline(
        DiarizeConfig(
            model="test-model", hf_token="test-token", device=configured_device
        )
    )

    assert result is loaded
    assert loaded.moved_to == expected_device


def test_assigns_community_one_output_to_words_and_segments(monkeypatch):
    class FakePipeline:
        def __call__(self, path, **kwargs):
            turns = [
                (SimpleNamespace(start=0.0, end=1.0), "SPEAKER_00"),
                (SimpleNamespace(start=1.0, end=2.0), "SPEAKER_01"),
            ]
            return SimpleNamespace(speaker_diarization=turns)

    monkeypatch.setattr(diarize_module, "_load_pipeline", lambda cfg: FakePipeline())
    transcript = Transcript(
        segments=[
            Segment(
                text="hello there",
                start=0.0,
                end=2.0,
                words=[
                    Word(text="hello", start=0.1, end=0.8),
                    Word(text="there", start=1.2, end=1.8),
                ],
            )
        ]
    )

    result = diarize_module.diarize("audio.wav", transcript, DiarizeConfig(hf_token="test"))
    assert result.has_speakers is True
    assert [word.speaker for word in result.segments[0].words] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]
    assert result.segments[0].speaker in {"SPEAKER_00", "SPEAKER_01"}


def test_assigns_nearest_turn_when_asr_timestamp_falls_in_vad_gap(monkeypatch):
    class FakePipeline:
        def __call__(self, path, **kwargs):
            return SimpleNamespace(
                speaker_diarization=[
                    (SimpleNamespace(start=0.0, end=1.0), "SPEAKER_00"),
                    (SimpleNamespace(start=3.0, end=4.0), "SPEAKER_01"),
                ]
            )

    monkeypatch.setattr(diarize_module, "_load_pipeline", lambda cfg: FakePipeline())
    transcript = Transcript(
        segments=[
            Segment(
                text="gap words",
                start=1.1,
                end=2.9,
                words=[
                    Word(text="gap", start=1.1, end=1.2),
                    Word(text="words", start=2.8, end=2.9),
                ],
            )
        ]
    )

    result = diarize_module.diarize("audio.wav", transcript, DiarizeConfig(hf_token="test"))
    assert result.has_speakers is True
    assert [word.speaker for word in result.segments[0].words] == [
        "SPEAKER_00",
        "SPEAKER_01",
    ]
    assert result.segments[0].speaker in {"SPEAKER_00", "SPEAKER_01"}


def test_group_speaker_segments_merges_consecutive_chinese_speech_and_words():
    transcript = Transcript(
        language="zh",
        provider="fake",
        model="model",
        has_speakers=True,
        segments=[
            Segment(
                text="今天先確認。",
                start=0.0,
                end=1.0,
                speaker="SPEAKER_00",
                words=[Word("今天先確認。", 0.0, 1.0, "SPEAKER_00")],
            ),
            Segment(
                text="接著開始處理。",
                start=1.4,
                end=2.5,
                speaker="SPEAKER_00",
                words=[Word("接著開始處理。", 1.4, 2.5, "SPEAKER_00")],
            ),
        ],
    )

    grouped, detail = diarize_module.group_speaker_segments(transcript)

    assert len(grouped.segments) == 1
    assert grouped.segments[0].text == "今天先確認。接著開始處理。"
    assert (grouped.segments[0].start, grouped.segments[0].end) == (0.0, 2.5)
    assert [(word.start, word.end) for word in grouped.segments[0].words] == [
        (0.0, 1.0),
        (1.4, 2.5),
    ]
    assert detail["merged_boundaries"] == 1
    assert detail["output_segments"] == 1


def test_group_speaker_segments_splits_word_level_turn_then_merges_next_run():
    transcript = Transcript(
        has_speakers=True,
        segments=[
            Segment(
                text="hello yes",
                start=0.0,
                end=1.8,
                speaker="SPEAKER_00",
                words=[
                    Word("hello", 0.0, 0.8, "SPEAKER_00"),
                    Word("yes", 1.0, 1.8, "SPEAKER_01"),
                ],
            ),
            Segment(
                text="indeed",
                start=2.0,
                end=2.7,
                speaker="SPEAKER_01",
                words=[Word("indeed", 2.0, 2.7, "SPEAKER_01")],
            ),
        ],
    )

    grouped, detail = diarize_module.group_speaker_segments(transcript)

    assert [(segment.speaker, segment.text) for segment in grouped.segments] == [
        ("SPEAKER_00", "hello"),
        ("SPEAKER_01", "yes indeed"),
    ]
    assert detail["split_boundaries"] == 1
    assert detail["merged_boundaries"] == 1
    assert [word.text for word in grouped.segments[1].words] == ["yes", "indeed"]


def test_group_speaker_segments_keeps_long_silence_as_a_new_paragraph():
    transcript = Transcript(
        has_speakers=True,
        segments=[
            Segment(text="first", start=0.0, end=1.0, speaker="SPEAKER_00"),
            Segment(text="second", start=4.1, end=5.0, speaker="SPEAKER_00"),
        ],
    )

    grouped, detail = diarize_module.group_speaker_segments(transcript)

    assert [segment.text for segment in grouped.segments] == ["first", "second"]
    assert detail["merged_boundaries"] == 0


def test_group_speaker_segments_preserves_unsafe_mixed_text_as_a_barrier():
    transcript = Transcript(
        has_speakers=True,
        segments=[
            Segment(
                text="punctuation must remain!",
                start=0.0,
                end=2.0,
                speaker="SPEAKER_00",
                words=[
                    Word("punctuation", 0.0, 0.8, "SPEAKER_00"),
                    Word("must remain", 1.0, 2.0, "SPEAKER_01"),
                ],
            ),
            Segment(text="next", start=2.1, end=2.5, speaker="SPEAKER_00"),
        ],
    )

    grouped, detail = diarize_module.group_speaker_segments(transcript)

    assert [segment.text for segment in grouped.segments] == [
        "punctuation must remain!",
        "next",
    ]
    assert grouped.segments[0].speaker is None
    assert grouped.has_speakers is False
    assert detail["unsafe_mixed_segments"] == 1
    assert detail["merged_boundaries"] == 0


def test_group_speaker_segments_is_idempotent():
    transcript = Transcript(
        has_speakers=True,
        segments=[
            Segment(text="one", start=0.0, end=1.0, speaker="SPEAKER_00"),
            Segment(text="two", start=1.1, end=2.0, speaker="SPEAKER_00"),
        ],
    )

    once, _detail = diarize_module.group_speaker_segments(transcript)
    twice, _detail = diarize_module.group_speaker_segments(once)

    assert twice == once


def test_group_speaker_segments_requires_exact_whitespace_reconstruction():
    transcript = Transcript(
        has_speakers=True,
        segments=[
            Segment(
                text="NewYork",
                start=0.0,
                end=2.0,
                speaker="SPEAKER_00",
                words=[
                    Word("New", 0.0, 0.8, "SPEAKER_00"),
                    Word("York", 1.0, 2.0, "SPEAKER_01"),
                ],
            )
        ],
    )

    grouped, detail = diarize_module.group_speaker_segments(transcript)

    assert grouped.segments[0].text == "NewYork"
    assert grouped.segments[0].speaker is None
    assert detail["unsafe_mixed_segments"] == 1


def test_group_speaker_segments_caps_continuous_monologues():
    transcript = Transcript(
        has_speakers=True,
        segments=[
            Segment(text="aaaa", start=0.0, end=1.0, speaker="SPEAKER_00"),
            Segment(text="bbbb", start=1.1, end=2.0, speaker="SPEAKER_00"),
            Segment(text="cccc", start=2.1, end=3.0, speaker="SPEAKER_00"),
        ],
    )

    grouped, detail = diarize_module.group_speaker_segments(
        transcript, max_chars=9, max_duration_seconds=120
    )

    assert [segment.text for segment in grouped.segments] == ["aaaa bbbb", "cccc"]
    assert detail["limit_boundaries"] == 1
