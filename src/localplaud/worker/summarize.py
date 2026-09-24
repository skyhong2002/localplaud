"""Generate notes using stored templates and a versioned local execution policy.

Plaud's visible template descriptions are retained as provenance, not mistaken
for its hidden generation prompts. Long Autopilot notes retain detailed sections.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from ..asr.base import Transcript as AsrTranscript
from ..config import Settings
from ..llm.base import LLMOutputInvalid, build_llm
from .note_policy import (
    AUTOPILOT_INSTRUCTIONS,
    NOTE_INSTRUCTIONS,
    NOTE_PROMPT_VERSION,
    SECTION_INSTRUCTIONS,
)
from .title_policy import TITLE_INSTRUCTIONS, TITLE_PROMPT_VERSION, has_template_title_leak

log = logging.getLogger(__name__)


def _llm_provider_model(settings: Settings) -> tuple[str, str | None]:
    provider = settings.llm.provider
    config = getattr(settings.llm, provider.replace("-", "_"), None)
    return provider, getattr(config, "model", None)


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


def _reduction_max_tokens(chunk_chars: int) -> int:
    """Budget reducer output so each hierarchy level can actually contract.

    For ordinary prose, one token can occupy roughly four characters.  Limiting
    output to about one third of the input group leaves room for conservative
    consolidation while ensuring that adjacent reduced notes can be grouped on
    the next round.  The provider still owns the exact tokenization.
    """
    return max(32, min(600, chunk_chars // 12))


def _summary_chunk_chars(settings: Settings, llm) -> int:
    """Use a provider's safe large-context budget when it advertises one."""
    provider_limit = getattr(llm, "summary_max_chunk_chars", None)
    if isinstance(provider_limit, int) and provider_limit > 0:
        return min(settings.pipeline.summary_chunk_chars, provider_limit)
    provider_budget = getattr(llm, "summary_chunk_chars", None)
    if isinstance(provider_budget, int) and provider_budget > 0:
        return max(settings.pipeline.summary_chunk_chars, provider_budget)
    return settings.pipeline.summary_chunk_chars


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

_OVERVIEW_REDUCE_PROMPT = """\
Extract brief evidence for an overview of this recording. The complete detailed
sections are retained separately and will be included unchanged in the final note.
Return at most 100 words (or 200 Chinese characters): the concrete main subjects,
key decisions and unresolved issues. Do not attempt to reproduce all details here.
Use only the evidence; distinguish proposals from decisions. No title or headings.

Detailed sections:
---
{text}
---
"""

_SUMMARY_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "description": TITLE_INSTRUCTIONS},
        "content_md": {"type": "string"},
        "tags": {
            "type": "object",
            "additionalProperties": False,
            "description": "High-signal archive labels grounded only in the transcript.",
            "properties": {
                "topics": {
                    "type": "array",
                    "description": "Two to five short subject labels.",
                    "items": {"type": "string"},
                },
                "people": {
                    "type": "array",
                    "description": "Named people only; never anonymous speaker labels.",
                    "items": {"type": "string"},
                },
                "orgs": {
                    "type": "array",
                    "description": "Named organizations, companies, teams, or groups.",
                    "items": {"type": "string"},
                },
            },
            "required": ["topics", "people", "orgs"],
        },
    },
    "required": ["title", "content_md", "tags"],
}

_TITLE_REPAIR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
}


def repair_recording_title(
    transcript: AsrTranscript,
    summary_content: str,
    rejected_title: str | None,
    settings: Settings,
) -> str | None:
    """Ask the selected summary model for a title-only repair.

    This is a typed output repair, not a replacement or modification of the
    stored Plaud template. It is used only when that template returned a
    category label instead of a recording-specific title.
    """
    llm = build_llm(settings.llm)
    evidence, _coverage = _prepare_source(transcript, settings, llm, title_only=True)
    prompt = f"""\
The previous title was rejected. Derive a new title from the transcript evidence.
Ignore the old title and note: they may contain template instructions as content.
Return one concise, recording-specific title grounded in the evidence. Name the
most concrete subject, event, people, or observable content. Never return labels
such as summary, transcript overview, recording summary, meeting summary, or
their Chinese equivalents.

{TITLE_INSTRUCTIONS}

Transcript evidence:
---
{evidence}
---
"""
    raw = llm.complete(
        prompt,
        system=(
            "You repair only a recording title. Do not rewrite the note or its Plaud "
            "template. Use the recording's dominant language; use Traditional Chinese "
            "with Taiwan wording for Chinese recordings."
        ),
        temperature=0.1,
        max_tokens=120,
        json_schema=_TITLE_REPAIR_SCHEMA,
    )
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw.strip() or None
    title = parsed.get("title") if isinstance(parsed, dict) else None
    return title.strip() if isinstance(title, str) and title.strip() else None


def generate_recording_title(transcript: AsrTranscript, settings: Settings) -> str | None:
    """Generate the first title as soon as a local transcript is durable.

    The primary Plaud-template note remains authoritative and may replace this
    provisional title later.  Keeping this call title-only prevents alignment,
    diarization, or correction failures from leaving a usable transcript unnamed.
    """
    llm = build_llm(settings.llm)
    evidence, _coverage = _prepare_source(transcript, settings, llm, title_only=True)
    raw = llm.complete(
        f"""\
Return one concise, recording-specific title grounded only in this transcript.
Name the most concrete subject, event, people, or observable content. Never
return labels such as summary, transcript overview, recording summary, meeting
summary, or their Chinese equivalents.

{TITLE_INSTRUCTIONS}

Transcript evidence:
---
{evidence}
---
""",
        system=(
            "You generate only a recording title. Use the recording's dominant "
            "language; use Traditional Chinese with Taiwan wording for Chinese "
            "recordings. Return no note or explanation."
        ),
        temperature=0.1,
        max_tokens=120,
        json_schema=_TITLE_REPAIR_SCHEMA,
    )
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw.strip() or None
    title = parsed.get("title") if isinstance(parsed, dict) else None
    return title.strip() if isinstance(title, str) and title.strip() else None


def _summary_output(raw: str) -> tuple[str | None, str, dict[str, list[str]] | None]:
    """Accept the typed one-call contract, with Markdown fallback for adapters
    that do not implement structured output."""
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        if raw.lstrip().startswith("{"):
            raise LLMOutputInvalid(
                "Summary returned incomplete or invalid structured output"
            ) from None
        return _extract_title(raw), raw, None
    if not isinstance(parsed, dict):
        return _extract_title(raw), raw, None
    content = parsed.get("content_md")
    title = parsed.get("title")
    if not isinstance(content, str) or not content.strip():
        raise LLMOutputInvalid("Summary returned no usable note content")
    from .tagging import normalize_tag_payload

    embedded_tags = (
        normalize_tag_payload(parsed["tags"]) if isinstance(parsed.get("tags"), dict) else None
    )
    return (
        title.strip() if isinstance(title, str) and title.strip() else _extract_title(content),
        content.strip(),
        embedded_tags,
    )


def _prepare_source(
    transcript: AsrTranscript,
    settings: Settings,
    llm,
    *,
    title_only: bool = False,
    detail_sections: list[str] | None = None,
) -> tuple[str, dict]:
    """Share full-transcript coverage between notes and title-only repair."""
    transcript_text = _render_transcript(transcript)
    original_chars = len(transcript_text)
    if title_only:
        # Whisper loops can otherwise outweigh an entire real conversation.
        # Keep every distinct substantive utterance, including the tail. Remove
        # repeated isolated fragments and count duplicate long lines only once.
        counts = Counter(seg.text.strip() for seg in transcript.segments)
        seen = set()
        lines = []
        for seg in transcript.segments:
            text = seg.text.strip()
            if not text or (counts[text] >= 3 and len(text) < 12):
                continue
            if text in seen:
                continue
            seen.add(text)
            lines.append(f"{seg.speaker}: {text}" if seg.speaker else text)
        transcript_text = "\n".join(lines)
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
            prompt = _COVERAGE_PROMPT.format(part=idx, total=len(chunks), text=chunk)
            if title_only:
                prompt = (
                    "Extract faithful coverage notes for naming this recording. List only "
                    "the concrete subjects, project names, events, decisions and goals in "
                    f"part {idx} of {len(chunks)}. Be concise; omit greetings and filler. "
                    f"Do not propose a title yet.\nTranscript part:\n---\n{chunk}\n---\n"
                )
            if detail_sections is not None:
                prompt = (
                    f"{SECTION_INSTRUCTIONS}\nTranscript portion {idx} of {len(chunks)}:\n"
                    f"---\n{chunk}\n---\n"
                )
            notes.append(
                llm.complete(
                    prompt,
                    system=(
                        NOTE_INSTRUCTIONS
                        if detail_sections is not None
                        else "You create loss-minimizing intermediate notes from one part of a "
                        "long transcript. Never invent facts. Reply in the source language."
                    ),
                    temperature=0.1,
                    max_tokens=240
                    if title_only
                    else (2400 if detail_sections is not None else 1200),
                )
            )
            map_calls += 1
        if detail_sections is not None:
            # Keep these sections verbatim. Only the overview/title evidence is
            # reduced; late details cannot disappear in a final compression pass.
            detail_sections.extend(notes)
        reduction_rounds = 0
        reduction_max_tokens = (
            max(1, min(600, chunk_chars // 8))
            if detail_sections is not None
            else _reduction_max_tokens(chunk_chars)
        )
        while len("\n\n".join(notes)) > chunk_chars:
            reduction_rounds += 1
            if reduction_rounds > 8:
                raise RuntimeError(
                    "hierarchical summary did not converge within 8 reduction rounds"
                )
            groups = _group_notes(notes, chunk_chars)
            notes = [
                llm.complete(
                    (
                        _OVERVIEW_REDUCE_PROMPT if detail_sections is not None else _REDUCE_PROMPT
                    ).format(text=group),
                    system="Preserve coverage while consolidating notes. Never invent facts.",
                    temperature=0.1,
                    max_tokens=reduction_max_tokens,
                )
                for group in groups
            ]
            reduce_calls += len(groups)
        source_text = (
            "The following are ordered coverage notes derived from every part of the "
            "complete transcript:\n\n" + "\n\n".join(notes)
        )
    return source_text, {
        "strategy": strategy,
        "transcript_chars": original_chars,
        "chunks": len(chunks),
        "map_calls": map_calls,
        "reduce_calls": reduce_calls,
    }


def summarize(
    transcript: AsrTranscript,
    settings: Settings,
    template_override: dict | None = None,
    *,
    context: dict | None = None,
    checkpoint_dir: Path | None = None,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Return titled notes with a separate, template-independent title contract."""
    from .summary_templates import (
        SummaryTemplate,
        get_effective_template,
        render_resolved_prompt,
        template_snapshot,
    )

    llm = build_llm(settings.llm)
    if template_override:
        resolved_template = SummaryTemplate(
            name=template_override["key"],
            version=int(template_override["version"]),
            display_name=template_override.get("name"),
            system=template_override.get("system_prompt") or None,
            instructions=template_override["instructions"],
            prompt_mode=template_override.get("prompt_mode", "structured"),
            provenance=template_override.get("provenance"),
        )
    else:
        resolved_template = get_effective_template(settings.pipeline.summary_template)
    from .transcript_quality import require_usable_transcript

    quality = require_usable_transcript(transcript)
    if settings.pipeline.note_quality == "evidence":
        from .evidence_notes import generate_evidence_notes

        result = generate_evidence_notes(
            transcript,
            settings,
            llm,
            template_snapshot(resolved_template),
            context=context,
            checkpoint_dir=checkpoint_dir,
            progress=progress,
        )
        result.setdefault("coverage", {})["transcript_quality"] = quality
        result["coverage"]["note_prompt_version"] = NOTE_PROMPT_VERSION
        result["coverage"]["title_prompt_version"] = TITLE_PROMPT_VERSION
        return result
    # A captured Autopilot description is not Plaud's hidden execution prompt.
    # Apply our explicit, versioned execution policy without changing that snapshot
    # or imposing this layout on a user-authored/specialist template.
    autopilot = (
        resolved_template.name == "plaud-autopilot"
        and resolved_template.provenance == "plaud-web-readonly"
        and resolved_template.prompt_mode == "direct"
    )
    sections: list[str] = []
    source_text, coverage = _prepare_source(
        transcript,
        settings,
        llm,
        detail_sections=sections if autopilot else None,
    )
    system, prompt = render_resolved_prompt(resolved_template, source_text)
    body_policy = NOTE_INSTRUCTIONS + ("\n" + AUTOPILOT_INSTRUCTIONS if autopilot else "")
    if sections:
        body_policy += (
            "\nThe detailed topic sections have already been written and will be attached "
            "unchanged. For content_md return ONLY a short substantive overview (one or "
            "two paragraphs) of the whole recording. Do not repeat the detailed sections "
            "or add a list of topics, action items or headings. Generate title and tags "
            "from the whole recording's evidence."
        )
    system = "\n\n".join(filter(None, [system, body_policy, TITLE_INSTRUCTIONS]))
    output_tokens = 800 if sections else 3000
    raw_content = llm.complete(
        prompt,
        system=system,
        temperature=0.2,
        max_tokens=output_tokens,
        json_schema=_SUMMARY_OUTPUT_SCHEMA,
    )
    title, content, embedded_tags = _summary_output(raw_content)
    if sections:
        content = "\n\n".join([content, *sections])
        coverage["strategy"] = "sectioned"
        coverage["detail_sections"] = len(sections)
    execution = {
        "version": NOTE_PROMPT_VERSION,
        "note_quality": "legacy",
        "instructions": body_policy,
        "section_instructions": SECTION_INSTRUCTIONS if sections else None,
        "output_tokens": output_tokens,
        "section_output_tokens": 2400 if sections else None,
        "chunk_chars": _summary_chunk_chars(settings, llm),
        "context_tokens": getattr(getattr(llm, "cfg", None), "context_tokens", None),
    }
    # Also enforce this on remote workers, before the result reaches the controller.
    # Do not hide an invalid explicit title by promoting an arbitrary note section.
    if has_template_title_leak(title):
        coverage["title_repair_calls"] = 1
        try:
            repaired = llm.complete(
                f"Return only a recording title.\n\n{TITLE_INSTRUCTIONS}"
                f"\nTranscript evidence:\n---\n{source_text}\n---\n",
                system="Name the recording from the evidence, never from template instructions.",
                temperature=0.1,
                max_tokens=120,
                json_schema=_TITLE_REPAIR_SCHEMA,
            )
            try:
                parsed = json.loads(repaired)
            except (TypeError, json.JSONDecodeError):
                parsed = {"title": repaired}
            title = parsed.get("title") if isinstance(parsed, dict) else None
        except Exception as exc:  # noqa: BLE001 - title failure must preserve usable notes
            coverage["title_repair_error"] = type(exc).__name__
    provider, model = _llm_provider_model(settings)
    # Extract typed tags from the default note on the same host that made the
    # summary (the WSL worker), so the controller never needs its own LLM call.
    from .tagging import extract_tags

    if resolved_template.name == settings.pipeline.summary_template:
        tags = embedded_tags if embedded_tags is not None else extract_tags(content, settings)
    else:
        tags = {}
    return {
        "title": title,
        "content_md": content,
        "provider": provider,
        "model": model,
        "tags": tags,
        "template": resolved_template.name,
        "template_version": resolved_template.version,
        "template_snapshot": {**template_snapshot(resolved_template), "execution": execution},
        "coverage": {
            **coverage,
            "title_prompt_version": TITLE_PROMPT_VERSION,
            "note_prompt_version": NOTE_PROMPT_VERSION,
        },
    }


def _extract_title(md: str) -> str | None:
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return None
