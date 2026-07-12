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
        "recording_instruction": (
            "Extract every explicit action item or commitment from this recording. "
            "Use a concise Markdown list. For each item include the task, owner, due "
            "date or timing, and status when stated. Write 'Not stated' for missing "
            "fields, distinguish proposals from commitments, and do not invent tasks."
        ),
        "library_instruction": (
            "Extract explicit action items and commitments across the retrieved recordings. "
            "Group them by recording and use a concise Markdown list. Include task, owner, "
            "due date or timing, and status when stated. Write 'Not stated' for missing "
            "fields, distinguish proposals from commitments, and do not invent tasks."
        ),
    },
    "task_table": {
        "key": "task_table",
        "version": 1,
        "name": "Task table",
        "description": "Build a structured table of grounded tasks and evidence.",
        "retrieval_query": "tasks assignments owners deadlines deliverables follow up",
        "recording_instruction": (
            "Create a Markdown table with columns Task, Owner, Due, Status, and "
            "Evidence. Include only tasks supported by the recording. Preserve "
            "uncertainty, label proposed work as Proposed, and use 'Not stated' "
            "rather than guessing missing owners or dates."
        ),
        "library_instruction": (
            "Create a Markdown table across the retrieved recordings with columns "
            "Recording, Task, Owner, Due, Status, and Evidence. Include only grounded tasks, "
            "preserve uncertainty, label proposed work as Proposed, and use 'Not stated' "
            "rather than guessing missing owners or dates."
        ),
    },
    "insights": {
        "key": "insights",
        "version": 1,
        "name": "Insights",
        "description": "Surface decisions, tensions, risks, and open questions.",
        "retrieval_query": "decisions insights risks tensions patterns unresolved questions",
        "recording_instruction": (
            "Identify the most useful grounded insights from this recording. Group "
            "them under Decisions, Patterns or tensions, Risks, and Open questions. "
            "Separate direct evidence from cautious inference, omit empty sections, "
            "and do not speculate beyond the cited transcript."
        ),
        "library_instruction": (
            "Identify useful grounded patterns across the retrieved recordings. Group them "
            "under Decisions, Cross-recording patterns or tensions, Risks, and Open "
            "questions. Name the supporting recordings, separate direct evidence from "
            "cautious inference, omit empty sections, and do not speculate beyond citations."
        ),
    },
}


def _for_scope(item: dict, scope: str) -> dict:
    if scope not in {"recording", "library"}:
        raise ValueError("Ask skill scope must be recording or library")
    result = deepcopy(item)
    result["scope"] = scope
    result["instruction"] = result.pop(f"{scope}_instruction")
    result.pop("recording_instruction", None)
    result.pop("library_instruction", None)
    return result


def list_ask_skills(scope: str = "recording") -> list[dict]:
    return [_for_scope(item, scope) for item in _SKILLS.values()]


def get_ask_skill(key: str, scope: str = "recording") -> dict:
    try:
        return _for_scope(_SKILLS[key], scope)
    except KeyError as exc:
        raise LookupError("unknown Ask quick action") from exc
