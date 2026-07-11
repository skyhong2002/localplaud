"""Named summary templates — Plaud-style multi-dimensional notes.

Each template pairs a system prompt with a markdown-section instructions
block; :func:`render_prompt` combines the instructions with the transcript
text into the full user prompt. The "default" template mirrors the prompt in
``summarize.py`` so behavior is unchanged when no template is chosen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Every system prompt shares the same ground rules Plaud-style notes need:
# stay faithful to the transcript, answer in its dominant language.
_GROUND_RULES = (
    "Never invent facts not present in the transcript. Reply in the "
    "transcript's dominant language."
)


@dataclass
class SummaryTemplate:
    name: str
    system: str
    instructions: str  # the markdown-section instructions block
    version: int = 1
    display_name: str | None = None


TEMPLATES: dict[str, SummaryTemplate] = {
    "default": SummaryTemplate(
        name="default",
        system=(
            "You are a meticulous meeting-notes assistant. You are given a "
            "transcript (possibly with speaker labels and multiple languages). "
            f"Produce clear, faithful notes. {_GROUND_RULES}"
        ),
        instructions="""\
# <a short descriptive title>

## Summary
A concise paragraph capturing what this recording is about.

## Key Points
- bullet points of the most important information

## Action Items
- concrete follow-ups, decisions, or TODOs (write "None" if there are none)""",
    ),
    "meeting": SummaryTemplate(
        name="meeting",
        system=(
            "You are a precise minute-taker for business meetings. You are "
            "given a transcript, possibly with speaker labels. "
            f"{_GROUND_RULES}"
        ),
        instructions="""\
# <a short descriptive meeting title>

## Attendees / Speakers
- speakers present (use names if stated, otherwise the speaker labels)

## Decisions
- decisions that were made (write "None" if there are none)

## Action Items
- concrete follow-ups, with an owner in parentheses when it can be inferred

## Open Questions
- unresolved questions or topics deferred to later (write "None" if there are none)""",
    ),
    "call": SummaryTemplate(
        name="call",
        system=(
            "You are an assistant summarizing a phone or video call from its "
            f"transcript. {_GROUND_RULES}"
        ),
        instructions="""\
# <a short descriptive call title>

## Purpose
One or two sentences on why this call happened.

## Key Points
- the most important information exchanged

## Commitments / Follow-ups
- who agreed to do what (write "None" if there are none)

## Sentiment
One sentence on the overall tone of the call, grounded in what was said.""",
    ),
    "lecture": SummaryTemplate(
        name="lecture",
        system=(
            "You are a study assistant turning a lecture or talk transcript "
            f"into revision notes. {_GROUND_RULES}"
        ),
        instructions="""\
# <a short descriptive lecture title>

## Topic
One sentence stating what the lecture covers.

## Key Concepts
- the main concepts, terms, and ideas introduced

## Summary
A concise paragraph tying the concepts together.

## Study Questions
- a few questions a student could use to test their understanding""",
    ),
    "personal": SummaryTemplate(
        name="personal",
        system=(
            "You are a personal assistant condensing a voice memo into a "
            f"short note for its author. {_GROUND_RULES}"
        ),
        instructions="""\
# <a short descriptive title>

## TL;DR
One or two sentences capturing the gist.

## Highlights
- the moments or thoughts worth remembering

## To-dos
- anything the author said they should do (write "None" if there are none)""",
    ),
}

_PROMPT_FRAME = """\
Summarize the following transcript as Markdown with exactly these sections:

{instructions}

Transcript:
---
{transcript}
---
"""


def get_template(name: str) -> SummaryTemplate:
    """Look up a template case-insensitively; unknown names fall back to "default"."""
    template = TEMPLATES.get(name.strip().lower())
    if template is None:
        log.warning("unknown summary template %r, falling back to 'default'", name)
        return TEMPLATES["default"]
    return template


def bootstrap_note_templates(session) -> None:
    """Seed built-ins once without overwriting later user-created versions."""
    from sqlalchemy import select

    from ..db.models import NoteTemplate

    existing = set(session.scalars(select(NoteTemplate.key)).all())
    for key, template in TEMPLATES.items():
        if key in existing:
            continue
        session.add(
            NoteTemplate(
                key=key,
                version=1,
                name=key.replace("-", " ").title(),
                system_prompt=template.system,
                instructions=template.instructions,
                is_builtin=True,
                is_active=True,
            )
        )


def get_effective_template(name: str) -> SummaryTemplate:
    """Resolve the active database version, with built-ins as a safe fallback."""
    key = name.strip().lower()
    try:
        from sqlalchemy import select

        from ..db.models import NoteTemplate
        from ..db.session import session_scope

        with session_scope() as session:
            row = session.scalar(
                select(NoteTemplate)
                .where(NoteTemplate.key == key, NoteTemplate.is_active.is_(True))
                .order_by(NoteTemplate.version.desc())
            )
            if row is not None:
                return SummaryTemplate(
                    name=row.key,
                    system=row.system_prompt,
                    instructions=row.instructions,
                    version=row.version,
                    display_name=row.name,
                )
    except Exception as exc:  # noqa: BLE001 - startup/standalone fallback
        log.debug("could not resolve database note template: %s", exc)
    return get_template(key)


def template_snapshot(template: SummaryTemplate) -> dict:
    return {
        "key": template.name,
        "version": template.version,
        "name": template.display_name or template.name.replace("-", " ").title(),
        "system_prompt": template.system,
        "instructions": template.instructions,
    }


def render_resolved_prompt(template: SummaryTemplate, transcript_text: str) -> tuple[str, str]:
    prompt = _PROMPT_FRAME.format(
        instructions=template.instructions, transcript=transcript_text
    )
    return template.system, prompt


def render_prompt(template_name: str, transcript_text: str) -> tuple[str, str]:
    """Return ``(system, full_user_prompt)`` for the named template."""
    return render_resolved_prompt(get_effective_template(template_name), transcript_text)
