"""Behavioral boundaries for conservative raw-ASR quality diagnostics."""

from __future__ import annotations

import copy
import json
import math

import pytest

from localplaud.asr.base import Segment, Transcript, Word
from localplaud.worker.transcript_quality import (
    TranscriptQualityError,
    assess_transcript,
    require_usable_transcript,
)


def _transcript(texts: list[str], *, duration: float, step: float = 2) -> Transcript:
    return Transcript(
        segments=[
            Segment(text=text, start=i * step, end=i * step + 1, speaker="SPEAKER_00")
            for i, text in enumerate(texts)
        ],
        duration=duration,
        language="en",
        provider="test",
    )


def _codes(assessment: dict) -> set[str]:
    return {issue["code"] for issue in assessment["issues"]}


def test_long_fixed_filler_loop_blocks_and_attaches_json_safe_assessment():
    transcript = _transcript(["Thank you. Yeah, okay, thanks!"] * 58, duration=29 * 60, step=30)

    with pytest.raises(TranscriptQualityError) as exc:
        require_usable_transcript(transcript)

    assessment = exc.value.assessment
    assert isinstance(exc.value, RuntimeError)
    assert "檢查音訊" in str(exc.value)
    assert "重試" in str(exc.value)
    assert "沒有內容" not in str(exc.value)
    assert assessment["version"] == "transcript-quality/v1"
    assert assessment["status"] == "degraded"
    assert assessment["blocking"] is True
    assert "fixed_filler_loop" in _codes(assessment)
    assert assessment["metrics"]["filler_token_ratio"] == 1.0
    json.dumps(assessment, allow_nan=False)


def test_real_short_thanks_is_usable():
    transcript = _transcript(["Thank you.", "Yeah, okay."], duration=8)
    assessment = require_usable_transcript(transcript)
    assert assessment["blocking"] is False
    assert "fixed_filler_loop" not in _codes(assessment)


def test_long_music_with_sparse_speech_only_warns():
    transcript = _transcript(["Thank you.", "好，謝謝。"], duration=29 * 60, step=800)
    assessment = require_usable_transcript(transcript)
    assert assessment["status"] == "degraded"
    assert assessment["blocking"] is False
    assert "sparse_text" in _codes(assessment)
    assert "long_gap" in _codes(assessment)


def test_silence_only_is_not_declared_unusable():
    assessment = require_usable_transcript(Transcript(segments=[], duration=300))
    assert assessment["blocking"] is False
    assert "fixed_filler_loop" not in _codes(assessment)


def test_multilingual_conversation_and_repetitive_substantive_speech_are_usable():
    conversation = _transcript(
        ["今天討論專案進度與明天的會議。", "We should review the release plan."] * 15,
        duration=300,
        step=9,
    )
    chant = _transcript(["Keep moving forward together!"] * 30, duration=300, step=9)
    for transcript in (conversation, chant):
        assessment = require_usable_transcript(transcript)
        assert assessment["blocking"] is False
        assert "fixed_filler_loop" not in _codes(assessment)
    assert "repeated_phrases" in _codes(assess_transcript(chant))


@pytest.mark.parametrize(
    "start,end",
    [(math.nan, 1.0), (1.0, math.inf), (2.0, 1.0), (-1.0, 1.0), ("bad", 1.0)],
)
def test_invalid_segment_timestamps_block(start, end):
    transcript = Transcript(segments=[Segment(text="hello", start=start, end=end)], duration=3)
    with pytest.raises(TranscriptQualityError) as exc:
        require_usable_transcript(transcript)
    assert "invalid_timestamps" in _codes(exc.value.assessment)
    json.dumps(exc.value.assessment, allow_nan=False)


def test_invalid_word_timestamps_and_duration_block():
    transcript = Transcript(
        segments=[
            Segment(text="hello", start=0, end=1, words=[Word(text="hello", start=0.8, end=0.2)])
        ],
        duration=math.inf,
    )
    assessment = assess_transcript(transcript)
    assert assessment["blocking"] is True
    assert {"invalid_timestamps", "invalid_duration"} <= _codes(assessment)
    json.dumps(assessment, allow_nan=False)


def test_assessment_preserves_every_input_field():
    transcript = Transcript(
        segments=[
            Segment(
                text="  Yeah, okay.  ",
                start=1.25,
                end=2.5,
                speaker=None,
                words=[Word(text="Yeah", start=1.25, end=1.7, confidence=0.7)],
            )
        ],
        duration=200,
        language="zh",
        provider="original",
        model="test-model",
        has_speakers=False,
    )
    original = copy.deepcopy(transcript)
    assess_transcript(transcript)
    require_usable_transcript(transcript)
    assert transcript == original
