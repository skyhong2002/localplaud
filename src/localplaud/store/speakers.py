"""Stable speaker identities for a recording.

Diarization emits stable labels (``SPEAKER_00`` …) inside the transcript
segment JSON. This module mirrors those labels into ``Speaker`` rows so the
user can attach an editable display name that survives reprocessing: syncing
only inserts missing keys and never deletes rows or overwrites a name.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Speaker


def speaker_keys_from_segments(segments: list[dict]) -> list[str]:
    """Distinct speaker keys from segment dicts, in order of first appearance.

    Looks at both segment-level ``speaker`` and nested word-level speakers.
    """
    keys: list[str] = []
    for seg in segments or []:
        sp = seg.get("speaker")
        if sp and sp not in keys:
            keys.append(sp)
        for word in seg.get("words") or []:
            wsp = word.get("speaker")
            if wsp and wsp not in keys:
                keys.append(wsp)
    return keys


def sync_speakers(session: Session, file_id: str, keys: list[str]) -> None:
    """Insert ``Speaker`` rows for any new keys. Never deletes rows and never
    touches an existing ``display_name`` — user renames must survive re-ASR
    and re-diarization."""
    existing = set(
        session.scalars(select(Speaker.key).where(Speaker.file_id == file_id))
    )
    for key in keys:
        if key not in existing:
            session.add(Speaker(file_id=file_id, key=key))


def display_names(session: Session, file_id: str) -> dict[str, str]:
    """Map speaker key -> user display name, only for renamed speakers."""
    rows = session.scalars(select(Speaker).where(Speaker.file_id == file_id))
    return {row.key: row.display_name for row in rows if row.display_name}
