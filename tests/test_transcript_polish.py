"""AI transcript polishing preserves the timed speaker structure."""

from __future__ import annotations

import json

import pytest

from localplaud.asr.base import Segment, Transcript, Word
from localplaud.config import Settings
from localplaud.llm.base import LLMError, LLMInputTooLarge, LLMTransientError
from localplaud.worker.polish import polish_transcript


class FakePolisher:
    name = "opencode-go"
    model = "qwen3.7-plus"

    def __init__(self):
        self.requests = []

    def available(self):
        return True

    def complete(self, prompt, **_kwargs):
        assert _kwargs["json_schema"]["required"] == ["segments"]
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


def test_polish_splits_structurally_invalid_multi_segment_chunks(monkeypatch):
    class SplittingPolisher(FakePolisher):
        def complete(self, prompt, **kwargs):
            request = json.loads(prompt)
            self.requests.append(request)
            if len(request["target_segments"]) > 1:
                return '{"segments":[]}'
            return json.dumps({"segments": request["target_segments"]})

    provider = SplittingPolisher()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    transcript = Transcript(
        segments=[
            Segment(text="first", start=0, end=1),
            Segment(text="second", start=1, end=2),
        ]
    )

    result = polish_transcript(transcript, Settings())

    assert [segment.text for segment in result["transcript"].segments] == [
        "first",
        "second",
    ]
    assert [len(request["target_segments"]) for request in provider.requests] == [2, 1, 1]
    assert result["detail"]["chunks"] == 2
    assert result["detail"]["attempts"] == 3
    assert result["detail"]["split_retries"] == 1
    assert result["detail"]["request_input_chars"] > len(transcript.text)
    assert result["detail"]["response_output_chars"] > result["detail"]["output_chars"]


def test_polish_does_not_split_transient_provider_failures(monkeypatch):
    class TimedOutPolisher(FakePolisher):
        def complete(self, prompt, **kwargs):
            self.requests.append(json.loads(prompt))
            raise LLMTransientError("provider timed out")

    provider = TimedOutPolisher()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    transcript = Transcript(
        segments=[
            Segment(text="first", start=0, end=1),
            Segment(text="second", start=1, end=2),
        ]
    )

    with pytest.raises(LLMTransientError, match="provider timed out"):
        polish_transcript(transcript, Settings())

    assert len(provider.requests) == 1


def test_polish_splits_provider_context_limit_errors(monkeypatch):
    class ContextLimitedPolisher(FakePolisher):
        def complete(self, prompt, **kwargs):
            request = json.loads(prompt)
            self.requests.append(request)
            if len(request["target_segments"]) > 1:
                raise LLMInputTooLarge("context window exceeded")
            return json.dumps({"segments": request["target_segments"]})

    provider = ContextLimitedPolisher()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    transcript = Transcript(
        segments=[
            Segment(text="first", start=0, end=1),
            Segment(text="second", start=1, end=2),
        ]
    )

    result = polish_transcript(transcript, Settings())

    assert [len(request["target_segments"]) for request in provider.requests] == [2, 1, 1]
    assert result["detail"]["attempts"] == 3
    assert result["detail"]["chunks"] == 2


def test_polish_uses_provider_specific_large_context_batch(monkeypatch):
    provider = FakePolisher()
    provider.polish_chunk_chars = 10_000
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    settings = Settings()
    settings.pipeline.polish_chunk_chars = 1_000
    transcript = Transcript(
        segments=[
            Segment(text=f"segment-{index}", start=index, end=index + 1)
            for index in range(80)
        ]
    )

    result = polish_transcript(transcript, settings)

    assert len(provider.requests) == 1
    assert result["detail"]["chunk_chars"] == 10_000
