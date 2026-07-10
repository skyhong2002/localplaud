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


def _intervals(segments: list[dict]) -> dict[str, list[tuple[float, float]]]:
    result: dict[str, list[tuple[float, float]]] = {}
    for segment in segments or []:
        speaker = segment.get("speaker")
        if speaker:
            result.setdefault(speaker, []).append(
                (float(segment.get("start") or 0), float(segment.get("end") or 0))
            )
    return result


def _overlap(left: list[tuple[float, float]], right: list[tuple[float, float]]) -> float:
    return sum(max(0.0, min(a_end, b_end) - max(a_start, b_start))
               for a_start, a_end in left for b_start, b_end in right)


def reconcile_speaker_keys(previous: list[dict], current: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Map run-local labels onto prior stable keys using conservative time overlap.

    A match needs at least 50% overlap with the new speaker's speaking time and a
    clear margin over the runner-up. Ambiguous identities keep a new stable key.
    """
    import copy

    old_intervals = _intervals(previous)
    new_intervals = _intervals(current)
    mapping: dict[str, str] = {}
    used: set[str] = set()
    candidates = []
    for new, intervals in new_intervals.items():
        duration = sum(end - start for start, end in intervals) or 1.0
        scores = sorted(
            ((_overlap(intervals, old_intervals[old]), old) for old in old_intervals),
            reverse=True,
        )
        if not scores:
            continue
        best, old = scores[0]
        second = scores[1][0] if len(scores) > 1 else 0.0
        if best / duration >= 0.5 and (second == 0 or best >= second * 1.5):
            candidates.append((best / duration, new, old))
    for _, new, old in sorted(candidates, reverse=True):
        if old not in used:
            mapping[new] = old
            used.add(old)

    occupied = set(old_intervals)
    next_index = 0
    for new in new_intervals:
        if new in mapping:
            continue
        if new not in occupied and new not in mapping.values():
            mapping[new] = new
            occupied.add(new)
            continue
        while f"SPEAKER_{next_index:02d}" in occupied:
            next_index += 1
        stable = f"SPEAKER_{next_index:02d}"
        mapping[new] = stable
        occupied.add(stable)

    reconciled = copy.deepcopy(current)
    for segment in reconciled:
        if segment.get("speaker") in mapping:
            segment["speaker"] = mapping[segment["speaker"]]
        for word in segment.get("words") or []:
            if word.get("speaker") in mapping:
                word["speaker"] = mapping[word["speaker"]]
    return reconciled, mapping


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
