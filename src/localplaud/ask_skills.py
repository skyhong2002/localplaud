"""Built-in, read-only grounded Ask quick actions.

These are prompt snapshots, not automation rules: running one creates an ordinary
Ask thread and never mutates notes, tasks, transcripts, or external systems.
"""

from __future__ import annotations

from copy import deepcopy

_SKILLS = {
    "action_items": {
        "key": "action_items",
        "version": 1,
        "name": "Action items",
        "description": "Extract commitments, owners, due dates, and next steps.",
        "retrieval_query": "action items commitments owners deadlines due dates next steps",
        "instruction": (
            "Extract every explicit action item or commitment from this recording. "
            "Use a concise Markdown list. For each item include the task, owner, due "
            "date or timing, and status when stated. Write 'Not stated' for missing "
            "fields, distinguish proposals from commitments, and do not invent tasks."
        ),
    },
    "task_table": {
        "key": "task_table",
        "version": 1,
        "name": "Task table",
        "description": "Build a structured table of grounded tasks and evidence.",
        "retrieval_query": "tasks assignments owners deadlines deliverables follow up",
        "instruction": (
            "Create a Markdown table with columns Task, Owner, Due, Status, and "
            "Evidence. Include only tasks supported by the recording. Preserve "
            "uncertainty, label proposed work as Proposed, and use 'Not stated' "
            "rather than guessing missing owners or dates."
        ),
    },
    "insights": {
        "key": "insights",
        "version": 1,
        "name": "Insights",
        "description": "Surface decisions, tensions, risks, and open questions.",
        "retrieval_query": "decisions insights risks tensions patterns unresolved questions",
        "instruction": (
            "Identify the most useful grounded insights from this recording. Group "
            "them under Decisions, Patterns or tensions, Risks, and Open questions. "
            "Separate direct evidence from cautious inference, omit empty sections, "
            "and do not speculate beyond the cited transcript."
        ),
    },
}


def list_ask_skills() -> list[dict]:
    return [deepcopy(item) for item in _SKILLS.values()]


def get_ask_skill(key: str) -> dict:
    try:
        return deepcopy(_SKILLS[key])
    except KeyError as exc:
        raise LookupError("unknown Ask quick action") from exc
