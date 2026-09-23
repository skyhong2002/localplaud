"""Summarize-stage helpers: transcript rendering and title extraction."""

from __future__ import annotations

from localplaud.asr.base import Segment, Transcript
from localplaud.worker.summarize import (
    _chunk_text,
    _extract_title,
    _reduction_max_tokens,
    _render_transcript,
    _summary_output,
)


def _transcript(*segs: Segment) -> Transcript:
    return Transcript(segments=list(segs))


# --------------------------------------------------------------------------- #
# _render_transcript
# --------------------------------------------------------------------------- #


def test_render_prefixes_speaker_labels():
    t = _transcript(
        Segment(text="hello there", start=0.0, end=1.0, speaker="SPEAKER_00"),
        Segment(text="hi back", start=1.0, end=2.0, speaker="SPEAKER_01"),
    )
    assert _render_transcript(t) == "SPEAKER_00: hello there\nSPEAKER_01: hi back"


def test_render_omits_prefix_when_no_speaker():
    t = _transcript(
        Segment(text="unlabelled", start=0.0, end=1.0),
        Segment(text="labelled", start=1.0, end=2.0, speaker="SPEAKER_00"),
    )
    lines = _render_transcript(t).splitlines()
    assert lines[0] == "unlabelled"  # no ": " prefix, no stray colon
    assert lines[1] == "SPEAKER_00: labelled"


def test_render_strips_segment_whitespace():
    t = _transcript(Segment(text="  padded text \n", start=0.0, end=1.0, speaker="A"))
    assert _render_transcript(t) == "A: padded text"


def test_render_truncates_at_max_chars_with_marker():
    t = _transcript(Segment(text="x" * 500, start=0.0, end=1.0))
    out = _render_transcript(t, max_chars=100)
    assert out.startswith("x" * 100)
    assert out.endswith("\n...[truncated]")
    # Only the marker follows the cut — nothing from the tail leaks through.
    assert out == "x" * 100 + "\n...[truncated]"


def test_render_no_marker_when_under_limit():
    t = _transcript(Segment(text="short", start=0.0, end=1.0))
    out = _render_transcript(t, max_chars=100)
    assert out == "short"
    assert "[truncated]" not in out


def test_render_empty_transcript():
    assert _render_transcript(_transcript()) == ""


def test_chunk_text_preserves_every_character():
    text = "first line\n" + "x" * 35 + "\nlast line"
    chunks = _chunk_text(text, 12)
    assert all(len(chunk) <= 12 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_reduction_budget_forces_hierarchy_to_contract():
    assert _reduction_max_tokens(6_000) == 500
    assert _reduction_max_tokens(50) == 32
    assert _reduction_max_tokens(100_000) == 600


def test_long_transcript_uses_every_chunk_before_final_summary(monkeypatch):
    from localplaud.config import Settings
    from localplaud.worker.summarize import summarize

    calls: list[tuple[str, dict]] = []

    class FakeLlm:
        def complete(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            if prompt.startswith("Extract faithful coverage notes"):
                return f"coverage note {len(calls)}"
            if prompt.startswith("Consolidate these ordered coverage notes"):
                return "consolidated coverage"
            return "# Complete note\n\n## Summary\nAll parts covered."

    monkeypatch.setattr("localplaud.worker.summarize.build_llm", lambda cfg: FakeLlm())
    transcript = _transcript(
        *(
            Segment(text=f"segment-{idx}-" + chr(65 + idx) * 35, start=idx, end=idx + 1)
            for idx in range(4)
        )
    )
    settings = Settings(pipeline={"summary_chunk_chars": 50, "summary_template": "plaud-meeting-minutes"})
    result = summarize(transcript, settings)

    map_prompts = [p for p, _ in calls if p.startswith("Extract faithful coverage notes")]
    reduce_calls = [
        kwargs
        for prompt, kwargs in calls
        if prompt.startswith("Consolidate these ordered coverage notes")
    ]
    assert len(map_prompts) == result["coverage"]["chunks"]
    assert result["coverage"]["strategy"] == "hierarchical"
    assert result["coverage"]["transcript_chars"] == len(_render_transcript(transcript))
    assert "[truncated]" not in "".join(map_prompts)
    assert reduce_calls and all(call["max_tokens"] == 32 for call in reduce_calls)
    assert result["title"] == "Complete note"


def test_large_context_provider_reduces_map_calls_without_dropping_text(monkeypatch):
    from localplaud.config import Settings
    from localplaud.worker.summarize import summarize

    class LargeContextLlm:
        summary_chunk_chars = 60_000

        def __init__(self):
            self.prompts = []

        def complete(self, prompt, **_kwargs):
            self.prompts.append(prompt)
            if prompt.startswith("Extract faithful coverage notes"):
                return "coverage"
            return "# Complete note\n\nFull coverage."

    llm = LargeContextLlm()
    monkeypatch.setattr("localplaud.worker.summarize.build_llm", lambda _cfg: llm)
    transcript = _transcript(
        Segment(text="A" * 59_000, start=0, end=1),
        Segment(text="B" * 59_000, start=1, end=2),
    )

    result = summarize(transcript, Settings(pipeline={"summary_chunk_chars": 6_000, "summary_template": "plaud-meeting-minutes"}))

    assert result["coverage"]["chunks"] == 2
    map_prompts = [p for p in llm.prompts if p.startswith("Extract faithful coverage notes")]
    assert len(map_prompts) == 2
    assert "A" * 1_000 in map_prompts[0]
    assert "B" * 1_000 in map_prompts[1]


def test_hyphenated_llm_provider_reports_configured_model(monkeypatch):
    from localplaud.config import Settings
    from localplaud.worker.summarize import summarize

    class FakeLlm:
        def complete(self, *_args, **_kwargs):
            return "# Result\n\n## Summary\nGrounded."

    monkeypatch.setattr("localplaud.worker.summarize.build_llm", lambda _cfg: FakeLlm())
    settings = Settings(llm={"provider": "opencode-go", "opencode_go": {"model": "qwen-tested"}})

    result = summarize(_transcript(Segment(text="evidence", start=0, end=1)), settings)

    assert result["provider"] == "opencode-go"
    assert result["model"] == "qwen-tested"


def test_reducer_converges_when_model_fills_each_token_budget(monkeypatch):
    from localplaud.config import Settings
    from localplaud.worker.summarize import summarize

    class BudgetFillingLlm:
        def complete(self, prompt, **kwargs):
            if prompt.startswith(("Extract faithful", "Consolidate these")):
                return "x" * (kwargs["max_tokens"] * 4)
            return "# Complete note\n\n## Summary\nAll parts covered."

    monkeypatch.setattr("localplaud.worker.summarize.build_llm", lambda cfg: BudgetFillingLlm())
    transcript = _transcript(
        *(Segment(text="x" * 5_990, start=idx, end=idx + 1) for idx in range(12))
    )

    result = summarize(transcript, Settings(pipeline={"summary_chunk_chars": 6_000, "summary_template": "plaud-meeting-minutes"}))

    assert result["coverage"]["chunks"] >= 12
    assert result["coverage"]["reduce_calls"] > result["coverage"]["chunks"]
    assert result["title"] == "Complete note"


# --------------------------------------------------------------------------- #
# _extract_title
# --------------------------------------------------------------------------- #


def test_extract_title_first_h1():
    md = "# Weekly Sync\n\n## Summary\nStuff happened.\n# Second Heading\n"
    assert _extract_title(md) == "Weekly Sync"


def test_extract_title_skips_leading_noise_and_strips():
    md = "\nsome preamble\n   #  Spaced Title   \nbody\n"
    assert _extract_title(md) == "Spaced Title"


def test_extract_title_ignores_deeper_headings():
    assert _extract_title("## Summary\n- point\n### Sub\n") is None


def test_extract_title_none_when_absent():
    assert _extract_title("plain text without headings") is None
    assert _extract_title("") is None


def test_typed_summary_output_keeps_title_separate_from_exact_template_markdown():
    title, content, tags = _summary_output(
        '{"title":"星期五新版上線會議","content_md":"## 會議摘要\\n\\n按計畫部署。",'
        '"tags":{"topics":["部署"],"people":[],"orgs":["產品團隊"]}}'
    )
    assert title == "星期五新版上線會議"
    assert content == "## 會議摘要\n\n按計畫部署。"
    assert tags == {"topic": ["部署"], "person": [], "org": ["產品團隊"]}


def test_typed_summary_reuses_embedded_tags_without_a_second_llm_turn(monkeypatch):
    from localplaud.config import Settings
    from localplaud.worker.summarize import summarize

    class OneTurnLlm:
        def __init__(self):
            self.calls = []

        def complete(self, prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            return (
                '{"title":"研究會議","content_md":"## 摘要\\n\\n討論研究設計。",'
                '"tags":{"topics":["研究設計"],"people":[],"orgs":[]}}'
            )

    llm = OneTurnLlm()
    monkeypatch.setattr("localplaud.worker.summarize.build_llm", lambda _cfg: llm)

    result = summarize(
        _transcript(Segment(text="討論研究設計", start=0, end=1)),
        Settings(),
    )

    assert len(llm.calls) == 1
    assert result["title"] == "研究會議"
    assert result["tags"] == {"topic": ["研究設計"], "person": [], "org": []}
    assert "tags" in llm.calls[0][1]["json_schema"]["required"]


def test_summary_output_falls_back_to_plain_markdown():
    title, content, tags = _summary_output("# Weekly Sync\n\n- Ship Friday")
    assert title == "Weekly Sync"
    assert content == "# Weekly Sync\n\n- Ship Friday"
    assert tags is None


def test_title_repair_uses_typed_title_only_contract(monkeypatch):
    from localplaud.config import Settings
    from localplaud.worker.summarize import repair_recording_title

    class FakeLlm:
        def __init__(self):
            self.calls = []

        def complete(self, prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            return '{"title":"重複片頭、欄目推廣與字幕署名"}'

    llm = FakeLlm()
    monkeypatch.setattr("localplaud.worker.summarize.build_llm", lambda _cfg: llm)

    title = repair_recording_title(
        _transcript(Segment(text="優優獨播劇場；中文字幕志願者", start=0, end=1)),
        "內容主要是重複片頭與字幕署名。",
        "轉錄內容概覽",
        Settings(),
    )

    assert title == "重複片頭、欄目推廣與字幕署名"
    assert "轉錄內容概覽" not in llm.calls[0][0]
    assert "優優獨播劇場" in llm.calls[0][0]
    assert llm.calls[0][1]["json_schema"]["required"] == ["title"]
    assert llm.calls[0][1]["max_tokens"] == 120


def test_title_repair_covers_tail_and_excludes_contaminated_note(monkeypatch):
    from localplaud.config import Settings
    from localplaud.worker.summarize import repair_recording_title

    calls = []

    class Llm:
        def complete(self, prompt, **kwargs):
            calls.append(prompt)
            if prompt.startswith("Extract faithful coverage notes"):
                return "launch decision" if "TAIL_DECISION" in prompt else "greeting"
            return '{"title":"Launch rollout decision"}'

    monkeypatch.setattr("localplaud.worker.summarize.build_llm", lambda _: Llm())
    title = repair_recording_title(
        _transcript(Segment(text="hello " * 900 + "TAIL_DECISION", start=0, end=90)),
        "CONTAMINATED_TEMPLATE_DESCRIPTION", "Autopilot 模板總結", Settings(),
    )
    assert title == "Launch rollout decision"
    assert "TAIL_DECISION" in "".join(calls)
    assert "CONTAMINATED_TEMPLATE_DESCRIPTION" not in "".join(calls)


def test_summary_repairs_template_title_without_rewriting_note(monkeypatch):
    import json

    from localplaud.config import Settings
    from localplaud.worker.summarize import summarize

    calls = []
    note = "## 決策\n週五部署新版。"

    class Llm:
        def complete(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            if len(calls) == 1:
                return json.dumps({"title": "Autopilot 模板總結：部署", "content_md": note,
                                   "tags": {"topics": ["部署"], "people": [], "orgs": []}})
            return '{"title":"新版部署：週五上線與驗收安排"}'

    monkeypatch.setattr("localplaud.worker.summarize.build_llm", lambda _: Llm())
    result = summarize(_transcript(Segment(text="週五部署新版", start=0, end=1)), Settings())
    assert result["title"] == "新版部署：週五上線與驗收安排"
    assert result["content_md"] == note
    assert result["coverage"]["title_repair_calls"] == 1
    assert result["coverage"]["title_prompt_version"] == "recording-title/v4"
    assert "Template names and descriptions are instructions" in calls[0][1]["system"]
    assert "Autopilot 模板提供" not in calls[1][0]


def test_title_evidence_ignores_short_asr_loops_but_preserves_substantive_tail(monkeypatch):
    from localplaud.config import Settings
    from localplaud.worker.summarize import generate_recording_title

    prompts = []

    class Llm:
        def complete(self, prompt, **kwargs):
            prompts.append(prompt)
            return '{"title":"晚餐點菜與場地安排"}'

    monkeypatch.setattr("localplaud.worker.summarize.build_llm", lambda _: Llm())
    transcript = _transcript(
        Segment(text="牛肉清湯與青菜各點一份", start=0, end=1),
        *(Segment(text="字幕署名", start=i, end=i+1) for i in range(1, 100)),
        Segment(text="最後確認下週表演的場地安排", start=100, end=101),
    )
    assert generate_recording_title(transcript, Settings()) == "晚餐點菜與場地安排"
    assert "字幕署名" not in prompts[0]
    assert "牛肉清湯" in prompts[0]
    assert "下週表演" in prompts[0]


def test_title_only_failure_returns_usable_note_for_persistence(monkeypatch):
    import json

    from localplaud.config import Settings
    from localplaud.worker.summarize import summarize

    class Llm:
        calls = 0

        def complete(self, prompt, **kwargs):
            self.calls += 1
            if self.calls > 1:
                raise TimeoutError("unavailable")
            return json.dumps({"title": "Autopilot 模板總結", "content_md": "## 決策\n週五發布",
                               "tags": {"topics": [], "people": [], "orgs": []}})

    monkeypatch.setattr("localplaud.worker.summarize.build_llm", lambda _: Llm())
    result = summarize(_transcript(Segment(text="週五發布", start=0, end=1)), Settings())
    assert result["content_md"] == "## 決策\n週五發布"
    assert result["coverage"]["title_repair_error"] == "TimeoutError"


def test_autopilot_keeps_early_and_late_details_outside_lossy_overview(monkeypatch):
    import json

    from localplaud.config import Settings
    from localplaud.worker.note_policy import NOTE_PROMPT_VERSION, SECTION_INSTRUCTIONS
    from localplaud.worker.summarize import summarize
    from localplaud.worker.summary_templates import TEMPLATES

    calls = []

    class Llm:
        def complete(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            if prompt.startswith(SECTION_INSTRUCTIONS):
                if 'TAIL_ACTION' in prompt:
                    return '## Permissions\n- [ ] Owner B must confirm write access.'
                return '## Testing\nProposed five trials; no date was agreed.'
            if prompt.startswith('Extract brief evidence for an overview'):
                return 'Discussed testing.'  # Deliberately loses the tail in the overview.
            return json.dumps({'title': 'Testing and permissions', 'content_md': 'Discussed testing.',
                               'tags': {'topics': ['testing'], 'people': [], 'orgs': []}})

    monkeypatch.setattr('localplaud.worker.summarize.build_llm', lambda _: Llm())
    transcript = _transcript(
        Segment(text='EARLY_PROPOSAL ' + 'x' * 60, start=0, end=30),
        Segment(text='TAIL_ACTION', start=30, end=60),
    )
    before = transcript.text
    result = summarize(transcript, Settings(pipeline={'summary_chunk_chars': 80}))
    assert result['coverage']['strategy'] == 'sectioned'
    assert result['coverage']['detail_sections'] == 2
    assert 'Proposed five trials; no date was agreed.' in result['content_md']
    assert 'Owner B must confirm write access.' in result['content_md']
    assert result['coverage']['note_prompt_version'] == NOTE_PROMPT_VERSION
    assert result['template_snapshot']['instructions'] == TEMPLATES['plaud-autopilot'].instructions
    assert result['template_snapshot']['execution']['version'] == NOTE_PROMPT_VERSION
    assert transcript.text == before
    assert 'ONLY a short substantive overview' in calls[-1][1]['system']


def test_custom_template_keeps_its_layout_and_has_execution_provenance(monkeypatch):
    import json

    from localplaud.config import Settings
    from localplaud.worker.summarize import summarize

    calls = []

    class Llm:
        def complete(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return json.dumps({'title': 'Metrics', 'content_md': '| Count |\n| --- |\n| 5 |',
                               'tags': {'topics': [], 'people': [], 'orgs': []}})

    monkeypatch.setattr('localplaud.worker.summarize.build_llm', lambda _: Llm())
    template = {'key': 'my-table', 'version': 3, 'instructions': 'Return only a metrics table.',
                'prompt_mode': 'direct', 'provenance': 'user'}
    result = summarize(_transcript(Segment(text='Count is five.', start=0, end=5)),
                       Settings(), template)
    assert result['content_md'].startswith('| Count |')
    assert template['instructions'] in calls[0][0]
    assert 'descriptive ## topic headings' not in calls[0][1]['system']
    assert result['template_snapshot']['version'] == 3
    assert result['template_snapshot']['execution']['section_instructions'] is None


def test_incomplete_structured_note_is_never_saved_as_markdown():
    import pytest

    from localplaud.llm.base import LLMOutputInvalid

    for raw in ('{"title":"Planning","content_md":"unfinished', '{"content_md":""}'):
        with pytest.raises(LLMOutputInvalid, match='Summary returned'):
            _summary_output(raw)


def test_markdown_fallback_can_start_with_timestamp_or_link():
    raw = '[00:30] Budget review\n- Approved the trial.'
    assert _summary_output(raw)[1] == raw


def test_sectioned_overview_contracts_even_when_reducer_fills_budget(monkeypatch):
    import json

    from localplaud.config import Settings
    from localplaud.worker.note_policy import SECTION_INSTRUCTIONS
    from localplaud.worker.summarize import summarize

    class Llm:
        def complete(self, prompt, **kwargs):
            if prompt.startswith(SECTION_INSTRUCTIONS):
                return 'x' * 90
            if prompt.startswith('Extract brief evidence for an overview'):
                return 'x' * (4 * kwargs['max_tokens'])
            return json.dumps({'title': 'Trial plan', 'content_md': 'Overview.',
                               'tags': {'topics': [], 'people': [], 'orgs': []}})

    monkeypatch.setattr('localplaud.worker.summarize.build_llm', lambda _: Llm())
    result = summarize(_transcript(Segment(text='e' * 600, start=0, end=60)),
                       Settings(pipeline={'summary_chunk_chars': 100}))
    assert result['coverage']['detail_sections'] == 6
    assert result['coverage']['reduce_calls'] > 0
    assert result['content_md'].count('x' * 90) == 6


def test_ollama_summary_cap_wins_over_global_chunk_budget():
    from localplaud.config import Settings
    from localplaud.llm.ollama import OllamaProvider
    from localplaud.worker.summarize import _summary_chunk_chars

    settings = Settings(pipeline={'summary_chunk_chars': 6000})
    assert _summary_chunk_chars(settings, OllamaProvider(settings.llm.ollama)) == 3000
    settings.pipeline.summary_chunk_chars = 1500
    assert _summary_chunk_chars(settings, OllamaProvider(settings.llm.ollama)) == 1500
