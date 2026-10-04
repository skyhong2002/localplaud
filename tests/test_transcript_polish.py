"""AI transcript polishing preserves the timed speaker structure."""

from __future__ import annotations

import json

import pytest

from localplaud.asr.base import Segment, Transcript, Word
from localplaud.config import Settings
from localplaud.llm.base import (
    LLMInputTooLarge,
    LLMTimeout,
    LLMTransientError,
)
from localplaud.worker.polish import _propose_corrections as polish_transcript


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
            Segment(
                text="好的",
                start=2.6,
                end=3.0,
                speaker="speaker-b",
                words=[Word(text="好的", start=2.6, end=3.0, speaker="speaker-b")],
            ),
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
    assert polished.segments[1].words == transcript.segments[1].words
    assert transcript.segments[0].words[0].start == 1.25
    assert transcript.segments[0].text == "我我今天開會"
    assert result["detail"]["changed_segment_ids"] == [0]
    assert result["provider"] == "opencode-go"
    assert result["model"] == "qwen3.7-plus"
    assert result["prompt_version"] == "transcript-polish/v5"


def test_polish_reports_chunk_and_segment_progress(monkeypatch):
    provider = FakePolisher()
    provider.polish_chunk_chars = 1_000
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    transcript = Transcript(
        segments=[
            Segment(text="x" * 600, start=0, end=1),
            Segment(text="y" * 600, start=1, end=2),
        ]
    )
    updates = []

    polish_transcript(transcript, Settings(), progress=updates.append)

    assert updates[0] == {
        "strategy": "contextual-segment-map",
        "segments": 2,
        "target_segments_total": 2,
        "target_segments_completed": 0,
        "chunks_completed": 0,
        "chunks_total": 2,
        "attempts": 0,
        "split_retries": 0,
        "kept_source_segments": 0,
        "last_split_reason": None,
    }
    assert updates[-1]["chunks_completed"] == 2
    assert updates[-1]["chunks_total"] == 2
    assert updates[-1]["target_segments_completed"] == 2
    assert updates[-1]["attempts"] == 2


def test_polish_preserves_empty_placeholders_without_sending_them_to_llm(monkeypatch):
    provider = FakePolisher()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    transcript = Transcript(
        language="zh",
        segments=[
            Segment(text="第一段", start=0, end=1),
            Segment(text="", start=1, end=1),
            Segment(text="第二段", start=1, end=2),
        ],
    )

    result = polish_transcript(transcript, Settings())

    assert [item["id"] for item in provider.requests[0]["target_segments"]] == [0, 2]
    assert [segment.text for segment in result["transcript"].segments] == [
        "第一段",
        "",
        "第二段",
    ]
    assert result["detail"]["skipped_empty_segments"] == 1


def test_polish_preserves_source_for_missing_segment_ids(monkeypatch):
    class BrokenPolisher(FakePolisher):
        def complete(self, prompt, **_kwargs):
            return '{"segments":[]}'

    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: BrokenPolisher())
    transcript = Transcript(segments=[Segment(text="hello", start=0, end=1)])
    result = polish_transcript(transcript, Settings())

    assert result["transcript"].segments[0].text == "hello"
    assert result["detail"]["kept_source_segments"] == 1
    assert result["detail"]["kept_missing_segments"] == 1
    assert result["detail"]["split_retries"] == 0


def test_polish_applies_valid_subset_and_preserves_only_omitted_segments(monkeypatch):
    class PartialPolisher(FakePolisher):
        def complete(self, prompt, **_kwargs):
            request = json.loads(prompt)
            first = request["target_segments"][0]
            return json.dumps({"segments": [{"id": first["id"], "text": "corrected first"}]})

    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: PartialPolisher())
    transcript = Transcript(
        segments=[
            Segment(text="raw first", start=0, end=1),
            Segment(text="raw second", start=1, end=2),
        ]
    )

    result = polish_transcript(transcript, Settings())

    assert [segment.text for segment in result["transcript"].segments] == [
        "corrected first",
        "raw second",
    ]
    assert result["detail"]["kept_missing_segments"] == 1
    assert result["detail"]["split_retries"] == 0


def test_polish_preserves_structurally_missing_multi_segment_chunks(monkeypatch):
    class SplittingPolisher(FakePolisher):
        def complete(self, prompt, **kwargs):
            request = json.loads(prompt)
            self.requests.append(request)
            return '{"segments":[]}'

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
    assert [len(request["target_segments"]) for request in provider.requests] == [2]
    assert result["detail"]["chunks"] == 1
    assert result["detail"]["attempts"] == 1
    assert result["detail"]["split_retries"] == 0
    assert result["detail"]["kept_missing_segments"] == 2
    assert result["detail"]["request_input_chars"] > len(transcript.text)
    assert result["detail"]["response_output_chars"] > result["detail"]["output_chars"]


def test_polish_still_splits_unexpected_segment_ids(monkeypatch):
    class UnexpectedIdPolisher(FakePolisher):
        def complete(self, prompt, **kwargs):
            request = json.loads(prompt)
            self.requests.append(request)
            if len(request["target_segments"]) > 1:
                return '{"segments":[{"id":999,"text":"wrong"}]}'
            return json.dumps({"segments": request["target_segments"]})

    provider = UnexpectedIdPolisher()
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
    assert result["detail"]["split_retries"] == 1
    assert result["detail"]["last_split_reason"] == (
        "transcript polish returned unexpected segment IDs"
    )


def test_polish_keeps_stubbornly_emptied_segments_without_splitting(monkeypatch):
    class EmptyingPolisher(FakePolisher):
        def complete(self, prompt, **kwargs):
            request = json.loads(prompt)
            self.requests.append(request)
            return json.dumps(
                {
                    "segments": [
                        {"id": item["id"], "text": ""} for item in request["target_segments"]
                    ]
                }
            )

    provider = EmptyingPolisher()
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
    assert [len(request["target_segments"]) for request in provider.requests] == [2]
    assert result["detail"]["split_retries"] == 0
    assert result["detail"]["kept_source_segments"] == 2
    assert result["detail"]["kept_emptied_segments"] == 2


def test_polish_keeps_filler_segments_the_model_empties_without_splitting(monkeypatch):
    class FillerDroppingPolisher(FakePolisher):
        def complete(self, prompt, **kwargs):
            request = json.loads(prompt)
            self.requests.append(request)
            return json.dumps(
                {
                    "segments": [
                        {
                            "id": item["id"],
                            "text": "" if len(item["text"]) <= 4 else item["text"],
                        }
                        for item in request["target_segments"]
                    ]
                },
                ensure_ascii=False,
            )

    provider = FillerDroppingPolisher()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    transcript = Transcript(
        language="zh",
        segments=[
            Segment(text="今天先對一下實驗設計", start=0, end=1, speaker="speaker-a"),
            Segment(text="呃", start=1, end=2, speaker="speaker-b"),
            Segment(text="對對對", start=2, end=3, speaker="speaker-a"),
        ],
    )

    result = polish_transcript(transcript, Settings())

    assert [segment.text for segment in result["transcript"].segments] == [
        "今天先對一下實驗設計",
        "呃",
        "對對對",
    ]
    assert len(provider.requests) == 1
    assert result["detail"]["split_retries"] == 0
    assert result["detail"]["kept_source_segments"] == 2


def test_polish_allows_shorter_text_and_empty_source_segments(monkeypatch):
    class ShorteningPolisher(FakePolisher):
        def complete(self, prompt, **kwargs):
            request = json.loads(prompt)
            return json.dumps(
                {
                    "segments": [
                        {"id": item["id"], "text": item["text"].replace("very ", "")}
                        for item in request["target_segments"]
                    ]
                }
            )

    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: ShorteningPolisher())
    transcript = Transcript(
        segments=[
            Segment(text="very concise", start=0, end=1),
            Segment(text="  ", start=1, end=2),
        ]
    )

    result = polish_transcript(transcript, Settings())

    assert [segment.text for segment in result["transcript"].segments] == ["concise", ""]


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
            Segment(text=f"segment-{index}", start=index, end=index + 1) for index in range(80)
        ]
    )

    result = polish_transcript(transcript, settings)

    assert len(provider.requests) == 1
    assert result["detail"]["chunk_chars"] == 10_000


def test_polish_remaps_complete_renumbered_ids(monkeypatch):
    """Local models sometimes renumber segments from 1; a complete, in-order
    renumbering is mapped back positionally instead of being discarded."""

    class RenumberingPolisher(FakePolisher):
        def complete(self, prompt, **kwargs):
            request = json.loads(prompt)
            self.requests.append(request)
            return json.dumps(
                {
                    "segments": [
                        {"id": position + 1, "text": item["text"].replace("我我", "我")}
                        for position, item in enumerate(request["target_segments"])
                    ]
                },
                ensure_ascii=False,
            )

    provider = RenumberingPolisher()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    transcript = Transcript(
        segments=[
            Segment(text="我我今天開會", start=0, end=1),
            Segment(text="好的", start=1, end=2),
        ]
    )

    result = polish_transcript(transcript, Settings())

    assert [segment.text for segment in result["transcript"].segments] == [
        "我今天開會",
        "好的",
    ]
    assert result["detail"]["remapped_renumbered_chunks"] == 1
    assert result["detail"]["split_retries"] == 0


def test_polish_keeps_source_when_single_segment_stays_invalid(monkeypatch):
    """One uncorrectable segment degrades to its original timed text instead of
    failing the whole stage; the single-segment chunk is retried exactly once."""

    class BrokenSegmentPolisher(FakePolisher):
        def complete(self, prompt, **kwargs):
            request = json.loads(prompt)
            self.requests.append(request)
            targets = request["target_segments"]
            if any(item["text"] == "毀損" for item in targets):
                return '{"segments":[{"id":7777,"text":"trunca'
            return json.dumps({"segments": targets}, ensure_ascii=False)

    provider = BrokenSegmentPolisher()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    transcript = Transcript(
        segments=[
            Segment(text="開場", start=0, end=1),
            Segment(text="毀損", start=1, end=2),
            Segment(text="結尾", start=2, end=3),
        ]
    )

    result = polish_transcript(transcript, Settings())

    assert [segment.text for segment in result["transcript"].segments] == [
        "開場",
        "毀損",
        "結尾",
    ]
    detail = result["detail"]
    assert detail["kept_invalid_segments"] == 1
    assert detail["kept_source_segments"] >= 1
    retried = [
        request
        for request in provider.requests
        if [item["text"] for item in request["target_segments"]] == ["毀損"]
    ]
    assert len(retried) == 2  # first single-segment attempt + one retry


def test_polish_token_budget_scales_with_target_text(monkeypatch):
    class BudgetProbe(FakePolisher):
        def __init__(self):
            super().__init__()
            self.budgets = []

        def complete(self, prompt, **kwargs):
            self.budgets.append(kwargs["max_tokens"])
            return super().complete(prompt, **kwargs)

    provider = BudgetProbe()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    long_text = "會議紀錄" * 500  # 2000 CJK chars ≈ well past a flat 2048-token budget
    transcript = Transcript(segments=[Segment(text=long_text, start=0, end=60)])

    polish_transcript(transcript, Settings())

    assert provider.budgets[0] >= len(long_text) * 2


def test_polish_splits_requests_that_time_out(monkeypatch):
    class SizeLimitedPolisher(FakePolisher):
        def complete(self, prompt, **kwargs):
            request = json.loads(prompt)
            self.requests.append(request)
            if len(request["target_segments"]) > 1:
                raise LLMTimeout("Codex CLI timed out after 900s")
            return json.dumps({"segments": request["target_segments"]})

    provider = SizeLimitedPolisher()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    transcript = Transcript(
        segments=[Segment(text=f"part {index}", start=index, end=index + 1) for index in range(3)]
    )

    result = polish_transcript(transcript, Settings())

    assert [len(request["target_segments"]) for request in provider.requests] == [3, 1, 2, 1, 1]
    assert result["detail"]["chunks"] == 3
    assert [segment.text for segment in result["transcript"].segments] == [
        "part 0",
        "part 1",
        "part 2",
    ]


def test_polish_fails_when_a_single_segment_times_out(monkeypatch):
    class TimedOutPolisher(FakePolisher):
        def complete(self, prompt, **kwargs):
            self.requests.append(json.loads(prompt))
            raise LLMTimeout("Codex CLI timed out after 900s")

    provider = TimedOutPolisher()
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    transcript = Transcript(
        segments=[Segment(text="first", start=0, end=1), Segment(text="second", start=1, end=2)]
    )

    with pytest.raises(LLMTimeout):
        polish_transcript(transcript, Settings())

    assert [len(request["target_segments"]) for request in provider.requests] == [2, 1]


def test_gateway_polish_chunk_default_stays_below_timeout_scale():
    from localplaud.config import AiGatewayLlmConfig

    assert AiGatewayLlmConfig().polish_chunk_chars <= 12_000


# --------------------------------------------------------------------------- #
# Overlapping chunk and review calls: faster, never different
# --------------------------------------------------------------------------- #


class ConcurrentPolisher:
    """Corrects 銀心 to 迎新 and approves every edit except those on ids divisible by 4."""

    name = "gateway"
    model = "scripted"
    supports_parallel_calls = True
    polish_chunk_chars = 1_000

    def __init__(self, *, stagger=True, timeout_first_large=False):
        import threading

        self._guard = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.stagger = stagger
        self.timeout_first_large = timeout_first_large
        self.timed_out = False

    def available(self):
        return True

    def complete(self, prompt, **kwargs):
        import time

        with self._guard:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            body = json.loads(prompt)
            if "target_segments" in body:
                ids = [item["id"] for item in body["target_segments"]]
                if self.timeout_first_large and len(ids) >= 2 and not self.timed_out:
                    self.timed_out = True
                    raise LLMTimeout("too large")
                if self.stagger:
                    time.sleep(0.04 * (30 - min(ids)) / 30 + 0.005)
                return json.dumps(
                    {
                        "segments": [
                            {"id": i["id"], "text": i["text"].replace("銀心", "迎新")}
                            for i in body["target_segments"]
                        ]
                    },
                    ensure_ascii=False,
                )
            if self.stagger:
                time.sleep(0.02)
            return json.dumps(
                {
                    "approved_ids": [p["id"] for p in body["proposals"] if p["id"] % 4],
                    "rejected": [
                        {"id": p["id"], "reason": "不支持此修改"}
                        for p in body["proposals"]
                        if not p["id"] % 4
                    ],
                },
                ensure_ascii=False,
            )
        finally:
            with self._guard:
                self.in_flight -= 1


def _meeting(count=30):
    return Transcript(
        language="zh",
        has_speakers=True,
        segments=[
            Segment(
                text=f"第{i}段銀心派對討論" + "內容" * 150,
                start=float(i),
                end=i + 0.9,
                speaker=f"S{i % 3}",
            )
            for i in range(count)
        ],
    )


def _polish(provider, parallelism, monkeypatch, **kwargs):
    from localplaud.worker.polish import polish_transcript as full_polish

    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)
    settings = Settings(pipeline={"polish_parallelism": parallelism})
    return full_polish(_meeting(), settings, **kwargs)


def test_parallel_correction_equals_sequential_correction(monkeypatch):
    sequential = _polish(ConcurrentPolisher(stagger=False), 1, monkeypatch)
    llm = ConcurrentPolisher()
    parallel = _polish(llm, 4, monkeypatch)

    assert llm.peak >= 2
    assert [s.text for s in sequential["transcript"].segments] == [
        s.text for s in parallel["transcript"].segments
    ]
    for key in (
        "chunks",
        "attempts",
        "split_retries",
        "kept_source_segments",
        "changed_segment_ids",
        "input_chars",
        "output_chars",
        "request_input_chars",
        "response_output_chars",
    ):
        assert sequential["detail"][key] == parallel["detail"][key], key
    seq_review, par_review = sequential["detail"]["review"], parallel["detail"]["review"]
    assert seq_review["decisions"] == par_review["decisions"]
    assert seq_review["proposals"] == par_review["proposals"]
    assert seq_review["calls"] == par_review["calls"] and seq_review["calls"] >= 2
    assert sequential["detail"]["chunks"] >= 5
    # Some edits are approved and some rejected, so the merge order is exercised.
    approved = {d["id"] for d in par_review["decisions"] if d["approve"]}
    assert approved and approved != {d["id"] for d in par_review["decisions"]}


def test_correction_overlap_is_bounded_and_needs_provider_opt_in(monkeypatch):
    llm = ConcurrentPolisher()
    _polish(llm, 2, monkeypatch)
    assert llm.peak == 2

    class SingleServer(ConcurrentPolisher):
        supports_parallel_calls = False

    single = SingleServer()
    _polish(single, 6, monkeypatch)
    assert single.peak == 1


def test_split_after_timeout_inside_a_parallel_worker_keeps_full_coverage(monkeypatch):
    llm = ConcurrentPolisher(timeout_first_large=True)
    updates = []
    result = _polish(llm, 4, monkeypatch, progress=updates.append)
    reference = _polish(ConcurrentPolisher(stagger=False), 1, monkeypatch)

    assert result["detail"]["split_retries"] == 1
    assert [s.text for s in result["transcript"].segments] == [
        s.text for s in reference["transcript"].segments
    ]
    assert len(result["transcript"].segments) == 30
    assert all(s.text for s in result["transcript"].segments)
    correction = [u for u in updates if u.get("strategy") == "contextual-segment-map"]
    assert all(u["chunks_total"] >= u["chunks_completed"] for u in correction)
    assert correction[-1]["target_segments_completed"] == 30
    assert correction[-1]["chunks_completed"] == correction[-1]["chunks_total"]


def test_budget_guard_runs_one_at_a_time_while_model_calls_overlap(monkeypatch):
    import threading
    import time

    guard_lock = threading.Lock()
    state = {"active": 0, "peak": 0, "calls": 0}

    def guard(usage):
        with guard_lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["calls"] += 1
        time.sleep(0.005)
        with guard_lock:
            state["active"] -= 1

    llm = ConcurrentPolisher()
    _polish(llm, 4, monkeypatch, dispatch_guard=guard)
    assert llm.peak >= 2
    assert state["peak"] == 1
    assert state["calls"] >= 6


def test_review_progress_never_moves_backwards_in_parallel(monkeypatch):
    updates = []
    _polish(ConcurrentPolisher(), 4, monkeypatch, progress=updates.append)
    review = [u["current"] for u in updates if u.get("phase") == "review"]
    assert review and review == sorted(review)
