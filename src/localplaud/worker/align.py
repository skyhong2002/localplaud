"""Honest word-timestamp alignment evidence for ASR transcripts.

The current Apple path uses word timestamps emitted by Whisper.  This module
validates that evidence as a durable pipeline stage without claiming that it is
wav2vec2/WhisperX forced alignment.  A future forced aligner can replace the
strategy behind the same artifact contract.
"""

from __future__ import annotations

import math

from ..asr.base import Transcript


class AlignmentUnavailable(RuntimeError):
    """The transcript has no usable word-level timing evidence."""


def inspect_word_alignment(transcript: Transcript) -> dict:
    words = [word for segment in transcript.segments for word in segment.words]
    if not words:
        raise AlignmentUnavailable(
            "ASR provider returned segment timestamps but no word timestamps"
        )
    previous_start = -1.0
    for index, word in enumerate(words):
        if not math.isfinite(word.start) or not math.isfinite(word.end):
            raise AlignmentUnavailable(f"word {index} has a non-finite timestamp")
        if word.start < 0 or word.end < word.start:
            raise AlignmentUnavailable(f"word {index} has an invalid timestamp range")
        if word.start < previous_start:
            raise AlignmentUnavailable(f"word {index} is not chronologically ordered")
        previous_start = word.start
    timed_segments = sum(bool(segment.words) for segment in transcript.segments)
    return {
        "strategy": "provider-word-timestamps",
        "forced_alignment": False,
        "word_count": len(words),
        "timed_segments": timed_segments,
        "segment_count": len(transcript.segments),
        "segment_coverage": (
            timed_segments / len(transcript.segments) if transcript.segments else 0.0
        ),
    }
