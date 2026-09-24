"""Contract tests with scripted completions; these do not prove model accuracy."""

import json
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
    assert "[00:00](/file/safe_1?t=0)" in result["content_md"]
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


def test_verifier_rejects_fabricated_owner_after_bounded_repairs():
    llm = Fake(bad_owner=True)
    with pytest.raises(LLMOutputInvalid, match="覆核.*虛構負責人"):
        note(llm)
    assert len([p for p, _ in llm.calls if p.startswith("從 target")]) == 3


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


def test_draft_reviewer_failure_is_bounded():
    llm = Fake(review_issues=True)
    with pytest.raises(LLMOutputInvalid, match="草稿覆核"):
        note(llm)
    assert len([p for p, _ in llm.calls if p.startswith("撰寫")]) == 3


def test_invalid_file_id_has_plain_timestamp():
    result = note(Fake(), context={"file_id": "../../escape"})
    assert "[00:00]" in result["content_md"]
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


def test_adjacent_citations_share_one_timestamp_without_losing_evidence():
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
    assert result["content_md"] == "共同來源 [00:00](/file/safe_1?t=0) 後續文字。"


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
