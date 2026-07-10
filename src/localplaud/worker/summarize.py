"""Summarization stage — turn a transcript into structured notes via an LLM.

Mirrors Plaud's "template note" idea: a titled markdown summary with key
points and action items. The template is a simple prompt; more templates can
be added later (meeting / call / lecture / ...).
"""

from __future__ import annotations

import logging

from ..asr.base import Transcript as AsrTranscript
from ..config import Settings
from ..llm.base import build_llm

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a meticulous meeting-notes assistant. You are given a transcript "
    "(possibly with speaker labels and multiple languages). Produce clear, "
    "faithful notes. Never invent facts not present in the transcript. Reply "
    "in the transcript's dominant language."
)

_TEMPLATE = """\
Summarize the following transcript as Markdown with exactly these sections:

# <a short descriptive title>

## Summary
A concise paragraph capturing what this recording is about.

## Key Points
- bullet points of the most important information

## Action Items
- concrete follow-ups, decisions, or TODOs (write "None" if there are none)

Transcript:
---
{transcript}
---
"""


def _render_transcript(transcript: AsrTranscript, max_chars: int = 24000) -> str:
    lines: list[str] = []
    for seg in transcript.segments:
        who = f"{seg.speaker}: " if seg.speaker else ""
        lines.append(f"{who}{seg.text.strip()}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"
    return text


def summarize(transcript: AsrTranscript, settings: Settings) -> dict:
    """Return {title, content_md, provider, model, template}."""
    from .summary_templates import render_prompt

    llm = build_llm(settings.llm)
    template = settings.pipeline.summary_template
    system, prompt = render_prompt(template, _render_transcript(transcript))
    content = llm.complete(prompt, system=system, temperature=0.2, max_tokens=1500)
    title = _extract_title(content)
    return {
        "title": title,
        "content_md": content,
        "provider": settings.llm.provider,
        "model": getattr(getattr(settings.llm, settings.llm.provider, None), "model", None),
        "template": template,
    }


def _extract_title(md: str) -> str | None:
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return None
