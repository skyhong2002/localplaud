"""AI transcript polishing preserves the timed speaker structure."""

from __future__ import annotations

import json

import pytest

from localplaud.asr.base import Segment, Transcript, Word
from localplaud.config import Settings
from localplaud.llm.base import LLMError
from localplaud.worker.polish import polish_transcript


class FakePolisher:
    name = "opencode-go"
    model = "qwen3.7-plus"

    def __init__(self):
        self.requests = []

    def available(self):
        return True

    def complete(self, prompt, **_kwargs):
        request = json.loads(prompt)
        self.requests.append(request)
        return json.dumps(
            {
                "segments": [
                    {"id": item["id"], "text": item["text"].replace("我我", "我")}
                    for item in request["target_segments"]
                ]
            },
            ensure_ascii=False,
        )


def test_polish_preserves_ids_timestamps_speakers_and_words(monkeypatch):
    provider = FakePolisher()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    transcript = Transcript(
        language="zh",
        has_speakers=True,
        segments=[
            Segment(
                text="我我今天開會",
                start=1.25,
                end=2.5,
                speaker="speaker-a",
                words=[Word(text="我", start=1.25, end=1.4, speaker="speaker-a")],
            ),
            Segment(text="好的", start=2.6, end=3.0, speaker="speaker-b"),
        ],
    )
    result = polish_transcript(transcript, Settings())
    polished = result["transcript"]

    assert [item.text for item in polished.segments] == ["我今天開會", "好的"]
    assert [(item.start, item.end, item.speaker) for item in polished.segments] == [
        (1.25, 2.5, "speaker-a"),
        (2.6, 3.0, "speaker-b"),
    ]
    assert polished.segments[0].words[0].start == 1.25
    assert result["provider"] == "opencode-go"
    assert result["model"] == "qwen3.7-plus"
    assert result["prompt_version"] == "transcript-polish/v1"


def test_polish_rejects_missing_segment_ids(monkeypatch):
    class BrokenPolisher(FakePolisher):
        def complete(self, prompt, **_kwargs):
            return '{"segments":[]}'

    monkeypatch.setattr(
        "localplaud.worker.polish.build_llm", lambda _cfg: BrokenPolisher()
    )
    transcript = Transcript(segments=[Segment(text="hello", start=0, end=1)])
    with pytest.raises(LLMError, match="changed or omitted segment IDs"):
        polish_transcript(transcript, Settings())
