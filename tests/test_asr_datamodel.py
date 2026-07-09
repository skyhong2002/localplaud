"""Transcript/Segment/Word data model behaviour."""

from localplaud.asr.base import Segment, Transcript, Word


def _two_speaker_transcript() -> Transcript:
    seg_a = Segment(
        text="Hello there.",
        start=0.0,
        end=1.2,
        speaker="SPEAKER_00",
        words=[
            Word(text="Hello", start=0.0, end=0.5, speaker="SPEAKER_00", confidence=0.98),
            Word(text="there.", start=0.6, end=1.2, speaker="SPEAKER_00", confidence=0.95),
        ],
    )
    seg_b = Segment(
        text="Hi, how are you?",
        start=1.5,
        end=3.0,
        speaker="SPEAKER_01",
        words=[
            Word(text="Hi,", start=1.5, end=1.8, speaker="SPEAKER_01"),
            Word(text="how", start=1.9, end=2.1, speaker="SPEAKER_01"),
            Word(text="are", start=2.2, end=2.4, speaker="SPEAKER_01"),
            Word(text="you?", start=2.5, end=3.0, speaker="SPEAKER_01"),
        ],
    )
    return Transcript(
        segments=[seg_a, seg_b],
        language="en",
        duration=3.0,
        provider="test",
        has_speakers=True,
    )


def test_text_joins_segment_texts():
    t = _two_speaker_transcript()
    assert t.text == "Hello there.\nHi, how are you?"


def test_text_skips_blank_segments():
    t = _two_speaker_transcript()
    t.segments.insert(1, Segment(text="   ", start=1.2, end=1.5))
    assert t.text == "Hello there.\nHi, how are you?"


def test_speakers_unique_and_ordered():
    t = _two_speaker_transcript()
    # Repeat SPEAKER_00 later; it must not appear twice.
    t.segments.append(Segment(text="Good, thanks.", start=3.2, end=4.0, speaker="SPEAKER_00"))
    assert t.speakers == ["SPEAKER_00", "SPEAKER_01"]


def test_speakers_ignores_unlabelled_segments():
    t = Transcript(
        segments=[
            Segment(text="no speaker yet", start=0.0, end=1.0),
            Segment(text="labelled", start=1.0, end=2.0, speaker="SPEAKER_03"),
        ]
    )
    assert t.speakers == ["SPEAKER_03"]
    assert t.has_speakers is False  # default until diarization sets it
