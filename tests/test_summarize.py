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
    settings = Settings(pipeline={"summary_chunk_chars": 50})
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

    result = summarize(transcript, Settings(pipeline={"summary_chunk_chars": 6_000}))

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

    result = summarize(transcript, Settings(pipeline={"summary_chunk_chars": 6_000}))

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
    assert "Rejected title: 轉錄內容概覽" in llm.calls[0][0]
    assert llm.calls[0][1]["json_schema"]["required"] == ["title"]
    assert llm.calls[0][1]["max_tokens"] == 120
