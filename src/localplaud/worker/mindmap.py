"""Mind map stage — distill a transcript into a nested Markdown outline.

The outline is plain Markdown (one H1 root plus a nested bullet list) so the
Web App can render it as a collapsible tree and exports stay portable. Long
transcripts are covered with the same bounded map/reduce approach as
summarization — the tail is never truncated.
"""

from __future__ import annotations

import logging
import re

from ..asr.base import Transcript as AsrTranscript
from ..config import Settings
from ..llm.base import build_llm
from .summarize import (
    _TIGHTEN_SUFFIX,
    _chunk_text,
    _group_notes,
    _llm_provider_model,
    _reduction_max_tokens,
    _render_transcript,
    _summary_chunk_chars,
    reduce_notes,
)

log = logging.getLogger(__name__)

_SYSTEM = (
    "You turn a transcript into a faithful topic hierarchy. Never invent "
    "facts not present in the source. Reply in the transcript's dominant "
    "language. Preserve exact Speaker N labels when attributing speech or ownership; "
    "never infer identities, rename labels or replace them with generic roles."
)

_MAP_PROMPT = """\
Extract hierarchical outline notes from transcript part {part} of {total}.
Output only an indented Markdown bullet list ("- " items, two spaces per
nesting level) of the topics, subtopics, decisions, facts, names, numbers, and
action items in this part, keeping their original order. No prose paragraphs,
no headings, no invented content.

Transcript part:
---
{text}
---
"""

_REDUCE_PROMPT = """\
Consolidate these ordered outline notes into a shorter set of outline notes.
Merge duplicate topics but preserve every distinct decision, fact, name,
number, question, and action item. Output only an indented Markdown bullet
list. Do not invent information. Hard length limit: the whole reply must stay
under {limit} characters, so merge overlapping nodes instead of listing them twice.

Outline notes:
---
{text}
---
"""

_OUTLINE_PROMPT = """\
Build a mind map of the complete recording as a nested Markdown outline.

Rules:
- The first line is exactly one H1 heading naming the recording's central
  topic: "# <short topic>".
- Every other line is a bullet ("- ") indented by multiples of two spaces to
  express hierarchy, nested 2 to 4 levels deep.
- Keep node labels short (a few words each). No prose paragraphs, no
  numbering, no other headings, no code fences.
- Cover all major topics of the source material; never invent facts.
- Use the source's dominant language.
{context}
Source material:
---
{text}
---
"""

_SUMMARY_CONTEXT = """
An existing summary of the same recording follows; you may use its structure
and titles as hints, but the outline must stay grounded in the source
material:
---
{summary}
---
"""

_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)\n?```\s*$", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")


def _normalize_outline(raw: str) -> str:
    """Coerce LLM output into a clean outline: strip code fences, guarantee a
    single leading H1 root, and wrap any stray prose lines as bullets."""
    text = raw.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("LLM returned an empty mind map outline")

    out: list[str] = []
    root: str | None = None
    for line in lines:
        heading = _HEADING_RE.match(line.strip())
        if heading:
            if root is None and not out:
                root = heading.group(2).strip()
                continue
            # Extra headings become bullets one level per depth beyond H2.
            depth = max(0, len(heading.group(1)) - 2)
            out.append(f"{'  ' * depth}- {heading.group(2).strip()}")
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            level = len(bullet.group(1).replace("\t", "  ")) // 2
            out.append(f"{'  ' * level}- {bullet.group(2).strip()}")
            continue
        # Prose fallback: keep the content as a top-level bullet.
        out.append(f"- {line.strip()}")
    if not out:
        raise ValueError("mind map outline has a title but no nodes")
    return "\n".join([f"# {root or 'Mind map'}", *out])


def merge_outlines(outlines: list[str]) -> str:
    """Join per-part outlines into one tree without another model call.

    The first part's root names the map. Each later part becomes a top-level
    branch under its own root label, so distinct topics from every part of a
    long recording stay present and in order. Identical branch labels are kept
    once.
    """
    roots: list[str] = []
    branches: list[str] = []
    seen: set[str] = set()
    for outline in outlines:
        lines = outline.splitlines()
        root = lines[0][2:].strip() if lines and lines[0].startswith("# ") else "Mind map"
        roots.append(root)
        body = [line for line in lines[1:] if line.strip()]
        if len(outlines) > 1:
            body = [f"- {root}", *(f"  {line}" for line in body)]
        key = "\n".join(body)
        if key in seen:
            continue
        seen.add(key)
        branches.extend(body)
    if not branches:
        raise ValueError("merged mind map has no nodes")
    return "\n".join([f"# {roots[0]}", *branches])


def _count_nodes(content_md: str) -> int:
    """Bullet nodes plus the root topic."""
    return 1 + sum(1 for line in content_md.splitlines() if line.lstrip().startswith("- "))


def generate_mind_map(
    transcript: AsrTranscript, settings: Settings, summary_md: str | None = None
) -> dict:
    """Return {template, title, content_md, provider, model, detail}.

    ``content_md`` is a nested Markdown outline: a single H1 root followed by
    an indented bullet list. ``summary_md`` (an existing local summary) is
    optional structural context only — the outline is always built from the
    full transcript.
    """
    llm = build_llm(settings.llm)
    transcript_text = _render_transcript(transcript)
    chunk_chars = _summary_chunk_chars(settings, llm)
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
                    _MAP_PROMPT.format(part=idx, total=len(chunks), text=chunk),
                    system=_SYSTEM,
                    temperature=0.1,
                    max_tokens=1200,
                )
            )
            map_calls += 1
        reduction_max_tokens = _reduction_max_tokens(chunk_chars)

        def reduce(group: str, limit: int, previous: int | None) -> str:
            prompt = _REDUCE_PROMPT.format(text=group, limit=limit)
            if previous is not None:
                prompt += _TIGHTEN_SUFFIX.format(previous=previous, limit=limit)
            return llm.complete(
                prompt, system=_SYSTEM, temperature=0.1, max_tokens=reduction_max_tokens
            )

        notes, reduction = reduce_notes(notes, chunk_chars, reduce)
        reduce_calls += reduction["reduce_calls"]
        if not reduction["fits"]:
            # The provider ignores output limits, so the notes cannot be squeezed
            # into one call. Build one outline per bounded group and merge the
            # trees deterministically: complete coverage, guaranteed to finish.
            strategy = "hierarchical-merged"
            source_text = None
        else:
            source_text = (
                "The following are ordered outline notes derived from every part of the "
                "complete transcript:\n\n" + "\n\n".join(notes)
            )

    context = ""
    if summary_md and summary_md.strip():
        context = _SUMMARY_CONTEXT.format(summary=summary_md.strip())
    if source_text is not None:
        raw = llm.complete(
            _OUTLINE_PROMPT.format(context=context, text=source_text),
            system=_SYSTEM,
            temperature=0.2,
            max_tokens=1500,
        )
        content = _normalize_outline(raw)
    else:
        outlines = []
        for group in _group_notes(notes, chunk_chars):
            raw = llm.complete(
                _OUTLINE_PROMPT.format(
                    context=context,
                    text=(
                        "Ordered outline notes from one consecutive part of the "
                        "complete transcript:\n\n" + group
                    ),
                ),
                system=_SYSTEM,
                temperature=0.2,
                max_tokens=1500,
            )
            outlines.append(_normalize_outline(raw))
        content = merge_outlines(outlines)
    provider, model = _llm_provider_model(settings)
    return {
        "template": "mind_map",
        "title": None,
        "content_md": content,
        "provider": provider,
        "model": model,
        "detail": {
            "strategy": strategy,
            "transcript_chars": len(transcript_text),
            "chunks": len(chunks),
            "map_calls": map_calls,
            "reduce_calls": reduce_calls,
            "outline_nodes": _count_nodes(content),
            "merged_parts": len(_group_notes(notes, chunk_chars))
            if strategy == "hierarchical-merged"
            else None,
        },
    }
