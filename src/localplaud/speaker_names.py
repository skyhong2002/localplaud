"""Recently used, person-assigned speaker names for the naming UI.

The list only offers names a person typed or confirmed in the *active*
workspace. Workspace isolation comes from the ORM tenancy filter on
``Speaker``; names the voice matcher applied by itself (and nobody has since
edited) are excluded so an automatic guess is never presented as the user's
own vocabulary. Trashed recordings do not contribute.
"""

from __future__ import annotations

from sqlalchemy import inspect, select

from .db.models import PlaudFile, Speaker

RECENT_SPEAKER_NAMES_DEFAULT = 8
RECENT_SPEAKER_NAMES_MAX = 20
# Bound the scan: recent names come from the most recently edited speakers.
_SCAN_LIMIT = 500


def _automatic_speaker_ids(session, speaker_ids: list[int]) -> dict[int, str]:
    from .voice_identity import VoiceAssignment

    if not speaker_ids or not inspect(session.connection()).has_table(
        VoiceAssignment.__tablename__
    ):
        return {}
    rows = session.execute(
        select(VoiceAssignment.speaker_id, VoiceAssignment.applied_name).where(
            VoiceAssignment.speaker_id.in_(speaker_ids),
            VoiceAssignment.status == "applied",
        )
    )
    return {speaker_id: name for speaker_id, name in rows if name}


def recent_speaker_names(
    session, *, limit: int = RECENT_SPEAKER_NAMES_DEFAULT, exclude_file_id: str | None = None
) -> list[dict]:
    """Return up to ``limit`` distinct manual names, most recently assigned first.

    Names are whitespace-normalized and de-duplicated case-insensitively; the
    most recent spelling wins. ``exclude_file_id`` drops names coming only from
    that recording (its own speakers are already offered separately).
    """
    limit = max(1, min(int(limit), RECENT_SPEAKER_NAMES_MAX))
    query = (
        select(Speaker.id, Speaker.display_name, Speaker.file_id, Speaker.updated_at)
        .join(PlaudFile, PlaudFile.id == Speaker.file_id)
        .where(
            Speaker.display_name.is_not(None),
            Speaker.display_name != "",
            PlaudFile.is_trash.is_(False),
        )
        .order_by(Speaker.updated_at.desc(), Speaker.id.desc())
        .limit(_SCAN_LIMIT)
    )
    if exclude_file_id:
        query = query.where(Speaker.file_id != exclude_file_id)
    rows = session.execute(query).all()
    automatic = _automatic_speaker_ids(session, [row.id for row in rows])
    result: list[dict] = []
    seen: set[str] = set()
    for speaker_id, raw_name, _file_id, updated_at in rows:
        name = " ".join((raw_name or "").split())
        if not name:
            continue
        applied = automatic.get(speaker_id)
        if applied is not None and applied.strip() == raw_name.strip():
            continue  # still the voice matcher's guess, not a person's choice
        folded = name.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(
            {"name": name, "last_used": updated_at.isoformat() if updated_at else None}
        )
        if len(result) >= limit:
            break
    return result
