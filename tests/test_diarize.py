"""Speaker assignment compatibility for the current pyannote Community-1 API."""

from types import SimpleNamespace

from localplaud.asr.base import Segment, Transcript, Word
from localplaud.config import DiarizeConfig
from localplaud.worker import diarize as diarize_module


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
