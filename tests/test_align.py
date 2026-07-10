import sys
from types import SimpleNamespace

import pytest

from localplaud.asr.base import Segment, Transcript, Word
from localplaud.config import AlignConfig
from localplaud.worker.align import AlignmentUnavailable, align


def test_whisper_timestamp_alignment_validates_and_reports_words(tmp_path):
    transcript = Transcript(segments=[Segment(text="hello", start=0, end=1,
        words=[Word(text="hello", start=0.1, end=0.9)])], language="en")
    result, detail = align(tmp_path / "unused.wav", transcript, AlignConfig())
    assert result is transcript
    assert detail == {"method": "asr-word-timestamps", "words": 1, "forced": False}


def test_alignment_degrades_honestly_when_words_are_missing(tmp_path):
    transcript = Transcript(segments=[Segment(text="hello", start=0, end=1)])
    with pytest.raises(AlignmentUnavailable, match="no word timestamps"):
        align(tmp_path / "unused.wav", transcript, AlignConfig())


def test_whisperx_selects_explicit_model_by_detected_language(monkeypatch, tmp_path):
    seen = {}

    def load_align_model(**kwargs):
        seen.update(kwargs)
        return object(), {}

    def run_align(*args, **kwargs):
        return {"segments": [{"text": "你好", "start": 0.0, "end": 1.0,
                              "words": [{"word": "你好", "start": 0.1,
                                         "end": 0.9, "score": 0.9}]}]}

    monkeypatch.setitem(sys.modules, "whisperx", SimpleNamespace(
        load_align_model=load_align_model, align=run_align,
    ))
    transcript = Transcript(
        segments=[Segment(text="你好", start=0, end=1)], language="zh-TW"
    )
    result, detail = align(
        tmp_path / "audio.wav", transcript,
        AlignConfig(provider="whisperx", models_by_language={"zh": "zh-aligner"}),
    )
    assert seen["language_code"] == "zh-TW"
    assert seen["model_name"] == "zh-aligner"
    assert result.segments[0].words[0].text == "你好"
    assert detail["alignment_model"] == "zh-aligner"
