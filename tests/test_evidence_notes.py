"""Contract tests with scripted completions; these do not prove model accuracy."""

import json
import re
from types import SimpleNamespace

import pytest

from localplaud.asr.base import Segment, Transcript
from localplaud.llm.base import LLMOutputInvalid
from localplaud.worker.evidence_notes import generate_evidence_notes

TEMPLATE = {
    "key": "plaud-autopilot",
    "name": "Autopilot",
    "version": 1,
    "instructions": "topic prose",
    "system_prompt": "",
    "prompt_mode": "direct",
    "provenance": "plaud-web-readonly",
}


def settings(chunk=24000, repairs=2):
    return SimpleNamespace(
        pipeline=SimpleNamespace(note_evidence_chunk_chars=chunk, note_repair_attempts=repairs),
        llm=SimpleNamespace(provider="fake"),
    )


class Fake:
    name = "fake"
    summary_max_chunk_chars = 24000

    def __init__(
        self,
        *,
        fail_at=None,
        model="scripted",
        bad_owner=False,
        bad_citation=False,
        omit_citation=False,
        review_issues=False,
    ):
        self.cfg = SimpleNamespace(model=model, setting="one")
        self.calls = []
        self.fail_at = fail_at
        self.bad_owner = bad_owner
        self.bad_citation = bad_citation
        self.omit_citation = omit_citation
        self.review_issues = review_issues

    def complete(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.fail_at == len(self.calls):
            raise RuntimeError("transport outage")
        if prompt.startswith("修補上一版擷取"):
            payload = json.loads(prompt.split("\n", 1)[1])
            return json.dumps(
                {
                    "replace": [
                        {
                            "index": i,
                            "fact": {**fact, "owner": "虛構負責人" if self.bad_owner else None},
                        }
                        for i, fact in enumerate(payload["previous"]["facts"])
                    ],
                    "append": [],
                    "remove": [],
                    "skip": [],
                },
                ensure_ascii=False,
            )
        if prompt.startswith("從 target"):
            scope = json.loads(prompt.split("\n", 1)[1].split("\n請修正", 1)[0])
            facts = []
            for part in scope["target"]:
                text = part["text"]
                if text.strip():
                    facts.append(
                        {
                            "topic": "事件",
                            "text": text.strip(),
                            "kind": "proposal" if "提議" in text else "fact",
                            "status": "proposed" if "提議" in text else "reported",
                            "owner": "虛構負責人" if self.bad_owner else None,
                            "deadline": None,
                            "sources": [{"id": part["id"], "quote": text.strip()}],
                        }
                    )
            return json.dumps({"facts": facts, "skipped": []}, ensure_ascii=False)
        if "逐項完整掃描" in prompt or "比對草稿" in prompt:
            return json.dumps(
                {
                    "warnings": [],
                    "issues": ["虛構負責人"]
                    if self.bad_owner or (self.review_issues and "比對草稿" in prompt)
                    else [],
                },
                ensure_ascii=False,
            )
        if prompt.startswith("綜合全部議題"):
            return json.dumps({"title": "事件 A 與事件 B"})
        if prompt.startswith("規劃"):
            group = json.loads(prompt.split("\n", 1)[1])
            return json.dumps(
                {
                    "title": "事件 A 與事件 B",
                    "tags": {"topics": ["事件"], "people": [], "orgs": []},
                    "sections": [{"heading": "記錄", "fact_ids": [x["id"] for x in group]}],
                },
                ensure_ascii=False,
            )
        if prompt.startswith("撰寫"):
            batch = json.loads(prompt.split("\n", 1)[1].split("\n修正", 1)[0])
            lines = []
            for item in batch:
                f = item["fact"]
                citation = (
                    "[[unknown]]"
                    if self.bad_citation
                    else ""
                    if self.omit_citation
                    else f"[[{f['id']}]]"
                )
                lines.append(f"{f['text']} {citation}")
            return json.dumps({"content_md": "\n".join(lines)}, ensure_ascii=False)
        raise AssertionError(prompt[:80])


class FailOnRepair(Fake):
    """Transport outage on the first extraction repair: the rejection is checkpointed."""

    def complete(self, prompt, **kwargs):
        if prompt.startswith("修補上一版擷取"):
            self.calls.append((prompt, kwargs))
            raise RuntimeError("transport outage")
        return super().complete(prompt, **kwargs)


class FailOnDraftRepair(Fake):
    """Transport outage on the first draft repair: the rejected draft is checkpointed."""

    def complete(self, prompt, **kwargs):
        if prompt.startswith("撰寫") and "修正覆核問題" in prompt:
            self.calls.append((prompt, kwargs))
            raise RuntimeError("transport outage")
        return super().complete(prompt, **kwargs)


def note(llm, transcript=None, **kwargs):
    transcript = transcript or Transcript(
        segments=[Segment("事件 A 3m；提議事件 B 15m。", 0, 7, speaker="S0")]
    )
    return generate_evidence_notes(transcript, settings(), llm, TEMPLATE, **kwargs)


def test_full_source_tail_quotes_and_status_and_links():
    llm = Fake()
    transcript = Transcript(
        segments=[Segment("事件 A 3m；提議事件 B 15m。", 0, 7), Segment("結尾的完整內容。", 9, 11)]
    )
    result = note(llm, transcript, context={"file_id": "safe_1", "input_transcript_revision": 4})
    evidence = result["template_snapshot"]["evidence"]
    assert result["coverage"]["segments"] == 2
    assert result["coverage"]["parts"] == 2
    assert any("結尾的完整內容" in fact["text"] for fact in evidence["facts"])
    assert any(
        fact["status"] == "proposed" and "3m" in fact["text"] and "15m" in fact["text"]
        for fact in evidence["facts"]
    )
    assert "?t=" not in result["content_md"]
    assert "[[f" not in result["content_md"]
    assert evidence["chunks"][0]["audit_issues"] == []
    assert result["coverage"]["requests"] == len(llm.calls)


def test_oversized_utterance_keeps_tail_and_all_part_ids():
    llm = Fake()
    text = "甲內容。" * 2300 + "最後一句。"
    transcript = Transcript(segments=[Segment(text, 0, 90)])
    result = generate_evidence_notes(transcript, settings(), llm, TEMPLATE)
    facts = result["template_snapshot"]["evidence"]["facts"]
    assert result["coverage"]["parts"] > 1
    assert "".join(ref["quote"] for f in facts for ref in f["sources"]) == text
    assert facts[-1]["sources"][0]["id"].startswith("s0p")


def test_unresolved_audit_issues_publish_notes_with_visible_caveats():
    llm = Fake(bad_owner=True)
    result = note(llm)
    # Bounded repairs still run in full before the objection is carried forward.
    assert len([p for p, _ in llm.calls if p.startswith(("從 target", "修補上一版擷取"))]) == 3
    assert result["coverage"]["review_status"] == "accepted_with_issues"
    unresolved = result["coverage"]["unresolved_review_issues"]
    assert unresolved and unresolved[0]["phase"] == "audit"
    assert "虛構負責人" in unresolved[0]["message"]
    assert "## 覆核備註" in result["content_md"]
    assert result["content_md"].rstrip().endswith("- 虛構負責人")
    chunk = result["template_snapshot"]["evidence"]["chunks"][0]
    assert chunk["audit_issues"] == ["虛構負責人"]


def test_unknown_or_missing_citation_rejected():
    for flag in ("bad_citation", "omit_citation"):
        with pytest.raises(LLMOutputInvalid, match="引用"):
            note(Fake(**{flag: True}))


def test_resume_after_transport_error_and_identity_invalidation(tmp_path):
    directory = tmp_path / "private"
    llm = Fake(fail_at=3)
    with pytest.raises(RuntimeError, match="transport"):
        note(llm, checkpoint_dir=directory)
    resumed = Fake()
    result = note(resumed, checkpoint_dir=directory)
    assert result["coverage"]["cache_hits"] >= 2
    assert all(path.stat().st_mode & 0o077 == 0 for path in directory.iterdir())
    changed_model = note(Fake(model="other"), checkpoint_dir=directory)
    assert changed_model["coverage"]["cache_hits"] == 0
    changed_source = note(
        Fake(), Transcript(segments=[Segment("不同來源", 0, 1)]), checkpoint_dir=directory
    )
    assert changed_source["coverage"]["cache_hits"] == 0
    changed_config = Fake()
    changed_config.cfg.setting = "two"
    assert note(changed_config, checkpoint_dir=directory)["coverage"]["cache_hits"] == 0


def test_specialist_instructions_and_empty_source():
    specialist = {
        **TEMPLATE,
        "key": "custom",
        "instructions": "請用 SOAP 格式",
        "system_prompt": "專科結構",
    }
    llm = Fake()
    result = generate_evidence_notes(
        Transcript(segments=[Segment("確認症狀", 0, 1)]), settings(), llm, specialist
    )
    assert any("SOAP" in kw["system"] and "專科結構" in kw["system"] for p, kw in llm.calls)
    assert result["template"] == "custom"
    empty = generate_evidence_notes(
        Transcript(segments=[Segment("  ", 0, 1)]), settings(), Fake(), TEMPLATE
    )
    assert empty["coverage"]["requests"] == 0
    assert empty["template_snapshot"]["evidence"]["insufficient_reason"]


def test_distinct_event_numbers_and_proposals_reach_ledger_and_prompts():
    transcript = Transcript(
        segments=[
            Segment("事件 A 是 3m，已回報。", 0, 2),
            Segment("事件 B 提議改為 15m，尚未同意。", 3, 5),
        ]
    )
    llm = Fake()
    result = note(llm, transcript)
    facts = result["template_snapshot"]["evidence"]["facts"]
    assert any(
        "事件 A" in f["text"] and "3m" in f["text"] and f["status"] == "reported" for f in facts
    )
    assert any(
        "事件 B" in f["text"] and "15m" in f["text"] and f["status"] == "proposed" for f in facts
    )
    prompts = "\n".join(p for p, _ in llm.calls)
    assert "3m" in prompts and "15m" in prompts and "proposed" in prompts


def test_draft_reviewer_objections_are_bounded_then_published_as_caveats():
    llm = Fake(review_issues=True)
    result = note(llm)
    assert len([p for p, _ in llm.calls if p.startswith("撰寫")]) == 3
    assert result["coverage"]["review_status"] == "accepted_with_issues"
    assert [item["phase"] for item in result["coverage"]["unresolved_review_issues"]] == ["verify"]
    assert result["content_md"].count("## 覆核備註") == 1


def test_clean_review_leaves_no_caveat_section():
    result = note(Fake())
    assert "review_status" not in result["coverage"]
    assert "覆核備註" not in result["content_md"]


def test_notes_have_no_display_timestamp_without_valid_file_id():
    result = note(Fake(), context={"file_id": "../../escape"})
    assert "[00:00]" not in result["content_md"]
    assert "/file/" not in result["content_md"]


def test_progress_and_execution_contract():
    events = []
    result = note(Fake(), progress=events.append)
    assert {event["phase"] for event in events} == {"extract", "audit", "plan", "draft", "verify"}
    assert result["template_snapshot"]["execution"]["version"] == "evidence-notes/v2"
    assert result["template_snapshot"]["execution"]["note_quality"] == "evidence"


def test_small_provider_context_keeps_all_sources_without_optional_neighbor_overflow():
    fake = Fake()
    fake.summary_max_chunk_chars = 3000
    result = generate_evidence_notes(
        Transcript(segments=[Segment("x" * 401, 0, 10)]), settings(chunk=3000), fake, TEMPLATE
    )
    evidence = result["template_snapshot"]["evidence"]
    assert len(evidence["sources"]) == result["coverage"]["parts"]
    assert {s["id"] for s in evidence["sources"]} == {
        source["id"] for fact in evidence["facts"] for source in fact["sources"]
    }


def test_recording_date_is_in_model_context_and_phase_provenance():
    fake = Fake()
    result = note(
        fake, context={"recorded_at": "2026-09-01T13:00:00+08:00", "timezone": "Asia/Taipei"}
    )
    assert all("Asia/Taipei" in kwargs["system"] for _, kwargs in fake.calls)
    assert result["coverage"]["phases"]["verify"]["model"] == "scripted"


def test_default_summary_dispatches_to_evidence_without_plaud_artifacts(monkeypatch):
    from localplaud.config import Settings
    from localplaud.worker.summarize import summarize

    fake = Fake()
    monkeypatch.setattr("localplaud.worker.summarize.build_llm", lambda _: fake)
    result = summarize(
        Transcript(segments=[Segment("本地錄音提議事件 A，尚未同意。", 0, 5)]), Settings()
    )
    assert result["coverage"]["strategy"] == "evidence"
    assert result["coverage"]["transcript_quality"]["blocking"] is False
    assert result["template_snapshot"]["evidence"]["facts"][0]["status"] == "proposed"


def test_adjacent_citations_are_hidden_without_losing_evidence():
    class SharedPassage(Fake):
        def complete(self, prompt, **kwargs):
            result = super().complete(prompt, **kwargs)
            if prompt.startswith("從 target"):
                data = json.loads(result)
                fact = data["facts"][0]
                data["facts"].append({**fact, "text": "另一項事實"})
                return json.dumps(data)
            if prompt.startswith("撰寫"):
                batch = json.loads(prompt.split("\n", 1)[1])
                refs = "".join(f"[[{item['fact']['id']}]]" for item in batch)
                return json.dumps({"content_md": f"共同來源 {refs} 後續文字。"})
            return result

    result = note(SharedPassage(), context={"file_id": "safe_1"})
    assert len(result["template_snapshot"]["evidence"]["facts"]) == 2
    assert result["content_md"] == "共同來源 後續文字。"


def test_source_coverage_rejects_silently_unaccounted_segments():
    class MissingSource(Fake):
        def complete(self, prompt, **kwargs):
            result = super().complete(prompt, **kwargs)
            if prompt.startswith("從 target"):
                data = json.loads(result)
                data["facts"] = data["facts"][:-1]
                return json.dumps(data)
            return result

    with pytest.raises(LLMOutputInvalid):
        note(MissingSource())


def test_nonblocking_review_warnings_are_preserved_in_provenance():
    class Warnings(Fake):
        def complete(self, prompt, **kwargs):
            result = super().complete(prompt, **kwargs)
            if "逐項完整掃描" in prompt or "比對草稿" in prompt:
                return json.dumps({"issues": [], "warnings": ["次要用詞可再簡化"]})
            return result

    result = note(Warnings())
    warnings = result["coverage"]["review_warnings"]
    assert {item["phase"] for item in warnings} == {"audit", "verify"}


def test_retry_continues_rejected_extraction_and_retains_reviewed_progress(tmp_path):
    with pytest.raises(RuntimeError):
        note(FailOnRepair(bad_owner=True), checkpoint_dir=tmp_path)
    resumed = Fake()
    result = note(resumed, checkpoint_dir=tmp_path)
    first_prompt = resumed.calls[0][0]
    assert "修補上一版擷取" in first_prompt and "虛構負責人" in first_prompt
    assert all(f["owner"] is None for f in result["template_snapshot"]["evidence"]["facts"])
    cached = note(Fake(fail_at=1), checkpoint_dir=tmp_path)
    assert cached["coverage"]["requests"] == 0
    assert cached["content_md"] == result["content_md"]


def test_retry_continues_rejected_draft_without_reextracting(tmp_path):
    with pytest.raises(RuntimeError):
        note(FailOnDraftRepair(review_issues=True), checkpoint_dir=tmp_path)

    class RepairDraft(Fake):
        def complete(self, prompt, **kwargs):
            result = super().complete(prompt, **kwargs)
            if prompt.startswith("撰寫"):
                assert "上一版草稿" in prompt
                value = json.loads(result)
                value["content_md"] += " 覆核修正。"
                return json.dumps(value)
            return result

    resumed = RepairDraft()
    result = note(resumed, checkpoint_dir=tmp_path)
    assert resumed.calls[0][0].startswith("撰寫")
    assert result["coverage"]["cache_hits"] >= 3
    assert result["coverage"]["requests"] == 2


def test_rejected_feedback_does_not_cross_model_or_source_boundaries(tmp_path):
    with pytest.raises(RuntimeError):
        note(FailOnRepair(bad_owner=True), checkpoint_dir=tmp_path)
    changed = Fake(model="different")
    note(changed, checkpoint_dir=tmp_path)
    assert "修補上一版擷取" not in changed.calls[0][0]
    changed = Fake()
    note(changed, Transcript(segments=[Segment("新來源", 0, 1)]), checkpoint_dir=tmp_path)
    assert "修補上一版擷取" not in changed.calls[0][0]


def test_changed_review_prompt_requires_fresh_review(monkeypatch, tmp_path):
    from localplaud.worker import evidence_notes

    note(Fake(), checkpoint_dir=tmp_path)
    monkeypatch.setattr(evidence_notes, "_VERIFY_TASK", evidence_notes._VERIFY_TASK + "加強核對。")
    changed = Fake()
    note(changed, checkpoint_dir=tmp_path)
    assert any("比對草稿" in prompt for prompt, _ in changed.calls)


def test_each_explicit_retry_keeps_its_own_bounded_generation_budget(tmp_path):
    # Pipeline retry limits govern whether a later invocation is allowed. A
    # manual retry can continue feedback, but cannot loop forever in this call.
    for _ in range(2):
        llm = Fake(bad_owner=True)
        result = note(llm, checkpoint_dir=tmp_path)
        assert result["coverage"]["review_status"] == "accepted_with_issues"
        extraction_calls = [
            prompt for prompt, _ in llm.calls if prompt.startswith(("從 target", "修補上一版擷取"))
        ]
        assert 1 <= len(extraction_calls) <= 3


def test_targeted_fact_patch_keeps_unmentioned_facts_and_input_immutable():
    from localplaud.worker.evidence_notes import _fact_patch_validator

    scope = {"target": [{"id": f"s{i}p0", "text": f"來源 {i}"} for i in range(3)], "context": []}

    def fact(i):
        return {
            "topic": "議題",
            "text": f"來源 {i}",
            "kind": "fact",
            "status": "reported",
            "owner": None,
            "deadline": None,
            "sources": [{"id": f"s{i}p0", "quote": f"來源 {i}"}],
        }

    previous = {"facts": [fact(0), fact(1)], "skipped": [{"id": "s2p0", "reason": "原先略過"}]}
    before = json.dumps(previous)
    patched = _fact_patch_validator(scope, previous)(
        {
            "replace": [{"index": 0, "fact": {**fact(0), "topic": "修正議題"}}],
            "append": [fact(2)],
            "remove": [],
            "skip": [],
        }
    )
    assert patched["facts"][1] == previous["facts"][1]
    assert patched["facts"][0]["topic"] == "修正議題"
    assert len(patched["facts"]) == 3 and patched["skipped"] == []
    assert json.dumps(previous) == before
    for patch in (
        {"replace": [{"index": 9, "fact": fact(0)}], "append": [], "remove": [], "skip": []},
        {"replace": [], "append": [], "remove": [0], "skip": []},
        {
            "replace": [],
            "append": [{**fact(2), "sources": [{"id": "s2p0", "quote": "杜撰"}]}],
            "remove": [],
            "skip": [],
        },
    ):
        with pytest.raises(LLMOutputInvalid):
            _fact_patch_validator(scope, previous)(patch)


def test_patch_cache_survives_transport_failure_before_review(tmp_path):
    class CorrectPatch(Fake):
        def complete(self, prompt, **kwargs):
            result = super().complete(prompt, **kwargs)
            if prompt.startswith("修補上一版擷取"):
                patch = json.loads(result)
                for item in patch["replace"]:
                    item["fact"]["owner"] = None
                return json.dumps(patch)
            return result

    with pytest.raises(RuntimeError, match="transport"):
        note(CorrectPatch(bad_owner=True, fail_at=4), checkpoint_dir=tmp_path)
    resumed = Fake()
    result = note(resumed, checkpoint_dir=tmp_path)
    assert not any(p.startswith(("從 target", "修補上一版擷取")) for p, _ in resumed.calls)
    facts = result["template_snapshot"]["evidence"]["facts"]
    assert len(facts) == 1 and facts[0]["owner"] is None


def test_repair_reviews_receive_previous_issues_without_waiving_failures(tmp_path):
    with pytest.raises(RuntimeError):
        note(FailOnRepair(bad_owner=True), checkpoint_dir=tmp_path)
    resumed = Fake()
    note(resumed, checkpoint_dir=tmp_path)
    audit = next(p for p, _ in resumed.calls if "逐項完整掃描" in p)
    assert '"previous_issues":["虛構負責人"]' in audit
    assert "不得僅因已重試就放行" in audit

    with pytest.raises(RuntimeError):
        note(FailOnDraftRepair(review_issues=True), checkpoint_dir=tmp_path / "draft")

    class ChangedDraft(Fake):
        def complete(self, prompt, **kwargs):
            result = super().complete(prompt, **kwargs)
            if prompt.startswith("撰寫"):
                value = json.loads(result)
                value["content_md"] += " 修正後。"
                return json.dumps(value)
            return result

    resumed = ChangedDraft()
    note(resumed, checkpoint_dir=tmp_path / "draft")
    review = next(p for p, _ in resumed.calls if "比對草稿" in p)
    assert '"previous_issues":["虛構負責人"]' in review


def test_bad_quote_retry_identifies_reference_and_keeps_exact_validation():
    class QuoteRepair(Fake):
        def complete(self, prompt, **kwargs):
            result = super().complete(prompt, **kwargs)
            if prompt.startswith("從 target"):
                data = json.loads(result)
                data["facts"][0]["sources"][0]["quote"] = "不存在的引文"
                return json.dumps(data, ensure_ascii=False)
            if prompt.startswith("修補上一版擷取"):
                assert "第 1 項事實" in prompt
                assert "不存在的引文" in prompt
                payload = json.loads(prompt.split("\n", 1)[1])
                data = json.loads(result)
                data["replace"][0]["fact"]["sources"][0]["quote"] = payload["source"]["target"][0][
                    "text"
                ]
                return json.dumps(data, ensure_ascii=False)
            return result

    llm = QuoteRepair()
    assert note(llm)
    assert sum(p.startswith("從 target") for p, _ in llm.calls) == 1
    assert sum(p.startswith("修補上一版擷取") for p, _ in llm.calls) == 1


def test_invalid_quote_is_not_published_when_repairs_keep_bad_reference(tmp_path):
    class BadQuote(Fake):
        def complete(self, prompt, **kwargs):
            result = super().complete(prompt, **kwargs)
            if prompt.startswith("從 target"):
                data = json.loads(result)
                data["facts"][0]["sources"][0]["quote"] = "不存在的引文"
                return json.dumps(data, ensure_ascii=False)
            return result

    llm = BadQuote()
    with pytest.raises(LLMOutputInvalid, match="逐字引文不符"):
        note(llm, checkpoint_dir=tmp_path)
    assert len(llm.calls) == 3
    assert not list(tmp_path.glob("*.json"))


def test_quote_verification_ignores_chinese_script_but_not_wording():
    from localplaud.worker.evidence_notes import _quote_matches

    source = "第十六週的時候都要停下來。对，等等这样子。嗯。"
    assert _quote_matches("對，等等這樣子。", source)
    assert _quote_matches("对，等等这样子。", source)
    assert not _quote_matches("對，然後這樣子。", source)


# --------------------------------------------------------------------------- #
# Overlapping calls: faster, never different
# --------------------------------------------------------------------------- #


def many_source_transcript(count=12):
    return Transcript(
        segments=[
            Segment(
                f"事件 {i} 發生在第 {i} 週，" + "細節" * 280,
                i * 10,
                i * 10 + 9,
                speaker=f"S{i % 2}",
            )
            for i in range(count)
        ]
    )


def parallel_settings(parallelism):
    return SimpleNamespace(
        pipeline=SimpleNamespace(
            note_evidence_chunk_chars=24000,
            note_repair_attempts=2,
            note_parallelism=parallelism,
        ),
        llm=SimpleNamespace(provider="fake"),
    )


class Concurrent(Fake):
    """Declares overlap safe and records the highest number of calls in flight."""

    supports_parallel_calls = True

    def __init__(self, *, stagger=True, **kwargs):
        super().__init__(**kwargs)
        import threading

        self._guard = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.stagger = stagger

    def complete(self, prompt, **kwargs):
        import time

        with self._guard:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            if self.stagger:
                # Earlier chunks finish last so out-of-order completion is real.
                digits = [int(x) for x in re.findall(r"事件 (\d+) 發生", prompt)]
                time.sleep(0.05 * (12 - (digits[0] if digits else 0)) / 12 + 0.01)
            return super().complete(prompt, **kwargs)
        finally:
            with self._guard:
                self.in_flight -= 1


def run_notes(llm, parallelism, **kwargs):
    return generate_evidence_notes(
        many_source_transcript(), parallel_settings(parallelism), llm, TEMPLATE, **kwargs
    )


def test_parallel_notes_match_sequential_notes_exactly():
    sequential = run_notes(Concurrent(stagger=False), 1)
    parallel_llm = Concurrent()
    parallel = run_notes(parallel_llm, 4)
    assert parallel_llm.peak >= 2
    assert sequential["content_md"] == parallel["content_md"]
    assert sequential["title"] == parallel["title"]
    assert sequential["tags"] == parallel["tags"]
    seq_facts = sequential["template_snapshot"]["evidence"]["facts"]
    par_facts = parallel["template_snapshot"]["evidence"]["facts"]
    assert [f["id"] for f in par_facts] == [f"f{i + 1}" for i in range(len(par_facts))]
    assert seq_facts == par_facts
    assert (
        sequential["template_snapshot"]["evidence"]["chunks"]
        == (parallel["template_snapshot"]["evidence"]["chunks"])
    )
    for key in ("chunks", "requests", "input_chars", "output_chars"):
        assert sequential["coverage"][key] == parallel["coverage"][key]
    assert sequential["coverage"]["chunks"] >= 3


def test_parallelism_is_bounded_and_needs_provider_opt_in():
    llm = Concurrent()
    run_notes(llm, 2)
    assert 2 <= llm.peak <= 2

    class SingleServer(Concurrent):
        supports_parallel_calls = False

    single = SingleServer()
    run_notes(single, 6)
    assert single.peak == 1


def test_progress_does_not_move_backwards_while_chunks_finish_out_of_order():
    seen = []
    run_notes(Concurrent(), 4, progress=seen.append)
    last = {}
    for event in seen:
        assert event["current"] >= last.get(event["phase"], 0), event
        last[event["phase"]] = event["current"]
    assert {"extract", "audit", "plan", "draft", "verify"} <= set(last)


def test_unresolved_objections_are_reported_in_source_order_when_parallel():
    class Doubtful(Concurrent):
        def complete(self, prompt, **kwargs):
            result = super().complete(prompt, **kwargs)
            if "逐項完整掃描" in prompt:
                value = json.loads(result)
                chunk = re.search(r"事件 (\d+) 發生", prompt)
                value["issues"] = [f"疑點 {chunk.group(1)}"] if chunk else []
                return json.dumps(value, ensure_ascii=False)
            return result

    result = run_notes(Doubtful(), 4)
    unresolved = result["coverage"]["unresolved_review_issues"]
    batches = [item["batch"] for item in unresolved if item["phase"] == "audit"]
    assert batches == sorted(batches) and len(batches) >= 3
    assert result["coverage"]["review_status"] == "accepted_with_issues"


def test_failure_in_one_parallel_chunk_propagates_and_keeps_finished_work(tmp_path):
    class Breaks(Concurrent):
        def complete(self, prompt, **kwargs):
            if "事件 7 發生" in prompt and prompt.startswith("從 target"):
                raise RuntimeError("transport outage")
            return super().complete(prompt, **kwargs)

    with pytest.raises(RuntimeError, match="transport outage"):
        run_notes(Breaks(), 4, checkpoint_dir=tmp_path)
    assert list(tmp_path.glob("*.json"))
    resumed = Concurrent()
    result = run_notes(resumed, 4, checkpoint_dir=tmp_path)
    assert result["coverage"]["cache_hits"] > 0
    assert result["title"]
