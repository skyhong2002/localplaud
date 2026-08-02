"""Summary template registry: lookup, fallback, and prompt rendering."""

from localplaud.worker.summary_templates import (
    TEMPLATES,
    get_template,
    render_prompt,
    render_resolved_prompt,
)


def test_registry_templates_are_complete():
    assert {"default", "meeting", "call", "lecture", "personal"} <= set(TEMPLATES)
    assert {
        "plaud-autopilot",
        "plaud-key-metrics",
        "plaud-intent-analysis",
        "plaud-meeting-narrative",
        "plaud-lecture-deep-dive",
        "plaud-research-interview",
        "plaud-interview",
        "plaud-full-transcript",
        "plaud-meeting-minutes",
        "plaud-meeting-highlights",
    } <= set(TEMPLATES)
    for name, template in TEMPLATES.items():
        assert template.name == name
        assert (template.system or "").strip() or template.prompt_mode == "direct"
        assert template.instructions.strip()


def test_get_template_is_case_insensitive():
    assert get_template("MEETING") is TEMPLATES["meeting"]
    assert get_template("  Lecture ") is TEMPLATES["lecture"]


def test_get_template_falls_back_to_default():
    assert get_template("no-such-template") is TEMPLATES["default"]


def test_render_prompt_embeds_transcript_and_instructions():
    transcript = "SPEAKER_00: we decided to ship on Friday"
    system, prompt = render_prompt("meeting", transcript)
    assert system == TEMPLATES["meeting"].system
    assert transcript in prompt
    assert TEMPLATES["meeting"].instructions in prompt


def test_render_prompt_unknown_template_uses_default():
    system, prompt = render_prompt("bogus", "hello")
    assert system == TEMPLATES["default"].system
    assert TEMPLATES["default"].instructions in prompt
    assert "hello" in prompt


def test_plaud_template_uses_the_copied_prompt_without_local_structure():
    template = TEMPLATES["plaud-autopilot"]
    system, prompt = render_resolved_prompt(template, "SPEAKER_00: test transcript")

    assert template.prompt_mode == "direct"
    assert template.provenance == "plaud-web-readonly"
    assert system is None
    assert prompt.startswith(template.instructions)
    assert "SPEAKER_00: test transcript" in prompt
    assert "Summarize the following transcript as Markdown with exactly these sections" not in prompt
