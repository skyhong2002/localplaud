"""Read-only speaker-name suggestions from stored voice-identity evidence.

The matcher keeps uncertain voices anonymous. When its best candidate fell
below the confidence threshold (``unknown``) or failed the margin/vote checks
(``ambiguous``), the candidate is still useful to a person who can listen and
decide. This module only *reads* that stored evidence: it never calls a model,
never changes a name, and never lowers a threshold. A suggestion becomes a name
only when the user saves it, which goes through the normal batch naming path
and is then recorded here as a user confirmation (see ``record_confirmations``).
"""

from __future__ import annotations

import math

from sqlalchemy import inspect, select

from .db.models import PlaudFile, Speaker
from .voice_identity import VoiceAssignment, VoiceEvent
from .voice_matching import usable_name

SUGGESTIBLE = ("unknown", "ambiguous")


def _score(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) and -1.0 <= value <= 1.0 else None


def _suggestion(assignment: VoiceAssignment) -> dict | None:
    evidence = assignment.evidence
    if assignment.status not in SUGGESTIBLE or not isinstance(evidence, dict):
        return None
    name = evidence.get("name")
    score = _score(evidence.get("score"))
    if not isinstance(name, str) or not usable_name(name.strip()) or score is None:
        return None
    runner_up = _score(evidence.get("runner_up"))
    threshold = _score(evidence.get("threshold"))
    if assignment.status == "unknown":
        reason = "below_threshold"
    else:
        reason = "ambiguous"
    probability = _score(evidence.get("probability"))
    suggestion = {
        "name": name.strip(),
        "score": round(score, 4),
        "runner_up": None if runner_up is None else round(runner_up, 4),
        "margin": None if runner_up is None else round(score - runner_up, 4),
        "threshold": threshold,
        "status": assignment.status,
        "reason": reason,
    }
    if probability is not None:
        # Present only for calibrated matches; the older shape is unchanged.
        suggestion["probability"] = round(probability, 4)
    return suggestion


def speaker_suggestions(session, file_id: str) -> dict[str, dict]:
    """Return ``{speaker_key: suggestion}`` for anonymous speakers only."""
    if not inspect(session.connection()).has_table(VoiceAssignment.__tablename__):
        return {}
    recording = session.get(PlaudFile, file_id)
    if recording is None or recording.is_trash:
        return {}
    speakers = {
        row.id: row for row in session.scalars(select(Speaker).where(Speaker.file_id == file_id))
    }
    suggestions = {}
    for assignment in session.scalars(
        select(VoiceAssignment).where(VoiceAssignment.file_id == file_id)
    ):
        speaker = speakers.get(assignment.speaker_id)
        if speaker is None or (speaker.display_name or "").strip():
            continue
        suggestion = _suggestion(assignment)
        if suggestion is not None:
            suggestions[speaker.key] = suggestion
    return suggestions


def record_confirmations(session, file_id: str, keys, saved_names: dict) -> list[str]:
    """Record that the user accepted a voice suggestion as their own name.

    Only keys whose saved name still equals the stored candidate count; an
    edited name is an ordinary manual name. The assignment is marked
    ``user_confirmed`` (never ``applied``), so the identity service treats the
    name as an explicit user-provided reference rather than an automatic match,
    and the audit trail keeps the score that prompted the suggestion.
    """
    if not keys or not inspect(session.connection()).has_table(VoiceAssignment.__tablename__):
        return []
    confirmed = []
    speakers = {
        row.key: row for row in session.scalars(select(Speaker).where(Speaker.file_id == file_id))
    }
    for key in keys:
        speaker = speakers.get(key)
        name = saved_names.get(key)
        if speaker is None or not name:
            continue
        assignment = session.get(VoiceAssignment, speaker.id)
        suggestion = _suggestion(assignment) if assignment is not None else None
        if suggestion is None or suggestion["name"] != name:
            continue
        session.add(
            VoiceEvent(
                speaker_id=speaker.id,
                action="suggestion_confirmed",
                detail={
                    "name": name,
                    "source": "user",
                    "origin": "voice_suggestion",
                    "suggestion": suggestion,
                },
            )
        )
        assignment.status = "user_confirmed"
        confirmed.append(key)
    return confirmed


def automatic_names(session, file_id: str) -> dict[str, dict]:
    """Return ``{speaker_key: info}`` for names the voice matcher applied by itself.

    Only names still equal to what was applied count; a later edit by a person makes
    the name theirs. Used to mark inferred names in the naming dialog so they can be
    reviewed and corrected, never to change anything.
    """
    if not inspect(session.connection()).has_table(VoiceAssignment.__tablename__):
        return {}
    speakers = {
        row.id: row for row in session.scalars(select(Speaker).where(Speaker.file_id == file_id))
    }
    result = {}
    for assignment in session.scalars(
        select(VoiceAssignment).where(
            VoiceAssignment.file_id == file_id, VoiceAssignment.status == "applied"
        )
    ):
        speaker = speakers.get(assignment.speaker_id)
        if speaker is None or not assignment.applied_name:
            continue
        if (speaker.display_name or "").strip() != assignment.applied_name:
            continue
        evidence = assignment.evidence if isinstance(assignment.evidence, dict) else {}
        probability = _score(evidence.get("probability"))
        score = _score(evidence.get("score"))
        result[speaker.key] = {
            "name": assignment.applied_name,
            "probability": None if probability is None else round(probability, 4),
            "score": None if score is None else round(score, 4),
        }
    return result
