from __future__ import annotations

import pytest

from localplaud.asr.base import Segment, Transcript, Word
from localplaud.worker.align import AlignmentUnavailable, inspect_word_alignment


def test_provider_word_timestamps_are_validated_without_claiming_forced_alignment():
    transcript = Transcript(
        segments=[
            Segment(
                text="hello world",
                start=0,
                end=1,
                words=[
                    Word(text="hello", start=0.0, end=0.4),
                    Word(text="world", start=0.5, end=0.9),
                ],
            )
        ]
    )
    detail = inspect_word_alignment(transcript)
    assert detail == {
        "strategy": "provider-word-timestamps",
        "forced_alignment": False,
        "word_count": 2,
        "timed_segments": 1,
        "segment_count": 1,
        "segment_coverage": 1.0,
    }


@pytest.mark.parametrize(
    "words,match",
    [
        ([], "no word timestamps"),
        ([Word(text="bad", start=1.0, end=0.5)], "invalid timestamp"),
        (
            [Word(text="later", start=2, end=3), Word(text="earlier", start=1, end=2)],
            "chronologically ordered",
        ),
    ],
)
def test_missing_or_invalid_word_timestamps_are_actionable(words, match):
    transcript = Transcript(segments=[Segment(text="x", start=0, end=3, words=words)])
    with pytest.raises(AlignmentUnavailable, match=match):
        inspect_word_alignment(transcript)
