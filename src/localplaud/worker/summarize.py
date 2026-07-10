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


def _render_transcript(transcript: AsrTranscript, max_chars: int | None = None) -> str:
    lines: list[str] = []
    for seg in transcript.segments:
        who = f"{seg.speaker}: " if seg.speaker else ""
        lines.append(f"{who}{seg.text.strip()}")
    text = "\n".join(lines)
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"
    return text


def _chunk_text(text: str, max_chars: int) -> list[str]:
    """Split text without dropping content, preferring transcript line boundaries."""
    if max_chars < 1:
        raise ValueError("summary_chunk_chars must be positive")
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_chars:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            chunks.append(line[:max_chars].rstrip("\n"))
            line = line[max_chars:]
        if current and len(current) + len(line) > max_chars:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line
    if current or not chunks:
        chunks.append(current.rstrip("\n"))
    return chunks


def _group_notes(notes: list[str], max_chars: int) -> list[str]:
    groups: list[str] = []
    current: list[str] = []
    size = 0
    bounded_notes = [part for note in notes for part in _chunk_text(note, max_chars)]
    for note in bounded_notes:
        addition = len(note) + (2 if current else 0)
        if current and size + addition > max_chars:
            groups.append("\n\n".join(current))
            current, size = [], 0
        current.append(note)
        size += len(note) + (2 if len(current) > 1 else 0)
    if current:
        groups.append("\n\n".join(current))
    return groups


_COVERAGE_PROMPT = """\
Extract faithful coverage notes from transcript part {part} of {total}.
Preserve decisions, facts, names, numbers, questions, and action items. Keep the
original sequence and speaker labels where useful. Do not write a final title and
do not omit material merely because it seems less important.

Transcript part:
---
{text}
---
"""

_REDUCE_PROMPT = """\
Consolidate these ordered coverage notes into a shorter, faithful set of coverage
notes. Preserve every distinct decision, fact, name, number, question, and action
item. Do not invent information and do not produce the final formatted summary.

Coverage notes:
---
{text}
---
"""


def summarize(transcript: AsrTranscript, settings: Settings) -> dict:
    """Return {title, content_md, provider, model, template}."""
    from .summary_templates import render_prompt

    llm = build_llm(settings.llm)
    template = settings.pipeline.summary_template
    transcript_text = _render_transcript(transcript)
    chunk_chars = settings.pipeline.summary_chunk_chars
    chunks = _chunk_text(transcript_text, chunk_chars)
    map_calls = 0
    reduce_calls = 0
    if len(chunks) == 1:
        source_text = chunks[0]
        strategy = "direct"
    else:
        strategy = "hierarchical"
        notes = []
        for idx, chunk in enumerate(chunks, start=1):
            notes.append(
                llm.complete(
                    _COVERAGE_PROMPT.format(part=idx, total=len(chunks), text=chunk),
                    system=(
                        "You create loss-minimizing intermediate notes from one part of a "
                        "long transcript. Never invent facts. Reply in the source language."
                    ),
                    temperature=0.1,
                    max_tokens=1200,
                )
            )
            map_calls += 1
        reduction_rounds = 0
        while len("\n\n".join(notes)) > chunk_chars:
            reduction_rounds += 1
            if reduction_rounds > 8:
                raise RuntimeError(
                    "hierarchical summary did not converge within 8 reduction rounds"
                )
            groups = _group_notes(notes, chunk_chars)
            notes = [
                llm.complete(
                    _REDUCE_PROMPT.format(text=group),
                    system="Preserve coverage while consolidating notes. Never invent facts.",
                    temperature=0.1,
                    max_tokens=1200,
                )
                for group in groups
            ]
            reduce_calls += len(groups)
        source_text = (
            "The following are ordered coverage notes derived from every part of the "
            "complete transcript:\n\n" + "\n\n".join(notes)
        )
    system, prompt = render_prompt(template, source_text)
    content = llm.complete(prompt, system=system, temperature=0.2, max_tokens=1500)
    title = _extract_title(content)
    return {
        "title": title,
        "content_md": content,
        "provider": settings.llm.provider,
        "model": getattr(getattr(settings.llm, settings.llm.provider, None), "model", None),
        "template": template,
        "coverage": {
            "strategy": strategy,
            "transcript_chars": len(transcript_text),
            "chunks": len(chunks),
            "map_calls": map_calls,
            "reduce_calls": reduce_calls,
        },
    }


def _extract_title(md: str) -> str | None:
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return None
