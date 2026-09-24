"""Automatic publication requires a complete source-grounded edit review."""

import json

import pytest

from localplaud.asr.base import Segment, Transcript
from localplaud.config import Settings
from localplaud.llm.base import LLMOutputInvalid, LLMTransientError
from localplaud.worker.polish import polish_transcript


class Reviewer:
    name = "fake"
    model = "test"

    def __init__(self, decisions=None, error=None):
        self.decisions = decisions
        self.error = error
        self.calls = []

    def available(self):
        return True

    def complete(self, prompt, **kw):
        body = json.loads(prompt)
        self.calls.append(body)
        if "target_segments" in body:
            return json.dumps(
                {
                    "segments": [
                        {
                            "id": s["id"],
                            "text": {0: "社團迎新派對", 1: "好"}.get(s["id"], s["text"]),
                        }
                        for s in body["target_segments"]
                    ]
                }
            )
        if self.error:
            raise self.error
        assert body["proposals"][0]["before"] == "社團銀心派對"
        return json.dumps(
            {
                "decisions": self.decisions
                if self.decisions is not None
                else [
                    {"id": 0, "approve": True, "reason": "社團活動支持迎新，同音錯字"},
                    {"id": 1, "approve": False, "reason": "原本片段不是肯定回答"},
                ]
            }
        )


def source():
    return Transcript(
        language="zh",
        segments=[
            Segment(text="社團銀心派對", start=0, end=1, speaker="a"),
            Segment(text="一下", start=2, end=3, speaker="b"),
            Segment(text="新生下週報到", start=4, end=5, speaker="a"),
        ],
    )


def test_automatic_review_applies_supported_edit_and_restores_unsupported_edit(monkeypatch):
    provider = Reviewer()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _: provider)
    original = source()
    reservations = []
    result = polish_transcript(original, Settings(), dispatch_guard=reservations.append)
    assert [s.text for s in result["transcript"].segments] == [
        "社團迎新派對",
        "一下",
        "新生下週報到",
    ]
    assert original.segments[0].text == "社團銀心派對"
    assert result["detail"]["review"]["rejected_segment_ids"] == [1]
    assert result["detail"]["changed_segment_ids"] == [0]
    assert result["detail"]["attempts"] == 2
    assert len(reservations) == 2
    assert all(x["input_chars"] > 0 and x["output_tokens"] > 0 for x in reservations)
    assert [(s.start, s.end, s.speaker) for s in original.segments] == [
        (s.start, s.end, s.speaker) for s in result["transcript"].segments
    ]


@pytest.mark.parametrize(
    "decisions",
    [
        [],
        [{"id": 0, "approve": True, "reason": "ok"}],
        [{"id": 0, "approve": "true", "reason": "ok"}],
        [{"id": True, "approve": True, "reason": "ok"}],
        [{"id": 0, "approve": True, "reason": "ok"}] * 2,
        [{"id": 99, "approve": True, "reason": "ok"}],
    ],
)
def test_incomplete_or_invalid_review_cannot_publish_candidate(monkeypatch, decisions):
    monkeypatch.setattr(
        "localplaud.worker.polish.build_llm", lambda _: Reviewer(decisions=decisions)
    )
    with pytest.raises(LLMOutputInvalid):
        polish_transcript(source(), Settings())


def test_reviewer_outage_does_not_silently_accept_candidate(monkeypatch):
    monkeypatch.setattr(
        "localplaud.worker.polish.build_llm", lambda _: Reviewer(error=LLMTransientError("timeout"))
    )
    with pytest.raises(LLMTransientError):
        polish_transcript(source(), Settings())


def test_unchanged_text_needs_no_edit_review(monkeypatch):
    provider = Reviewer()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _: provider)
    result = polish_transcript(
        Transcript(segments=[Segment(text="社團迎新派對", start=0, end=1)]), Settings()
    )
    assert result["detail"]["review"]["calls"] == 0
    assert len(provider.calls) == 1
