"""Stable speaker identities for a recording.

Diarization emits run-local labels (``SPEAKER_00`` …) inside the transcript
segment JSON. This module maps them onto durable ``Speaker`` rows so editable
display names survive reprocessing. Timestamp overlap provides the evidence;
uncertain voices receive new identities instead of inheriting a name.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Speaker

_MIN_OVERLAP_SECONDS = 0.5
_MIN_MATCH_SCORE = 0.5
_AMBIGUITY_MARGIN = 0.12


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


def _speaker_intervals(segments: list[dict]) -> dict[str, list[list[float]]]:
    """Return merged speech intervals, preferring word-level assignments."""
    raw: dict[str, list[tuple[float, float]]] = {}
    for segment in segments or []:
        words = [word for word in segment.get("words") or [] if word.get("speaker")]
        items = words or ([segment] if segment.get("speaker") else [])
        for item in items:
            start, end = item.get("start"), item.get("end")
            if start is None or end is None or float(end) <= float(start):
                continue
            raw.setdefault(item["speaker"], []).append((float(start), float(end)))
    merged: dict[str, list[list[float]]] = {}
    for key, intervals in raw.items():
        output: list[list[float]] = []
        for start, end in sorted(intervals):
            if output and start <= output[-1][1] + 0.05:
                output[-1][1] = max(output[-1][1], end)
            else:
                output.append([start, end])
        merged[key] = output
    return merged


def _duration(intervals: list[list[float]]) -> float:
    return sum(end - start for start, end in intervals)


def _overlap(left: list[list[float]], right: list[list[float]]) -> float:
    total = 0.0
    for left_start, left_end in left:
        for right_start, right_end in right:
            total += max(0.0, min(left_end, right_end) - max(left_start, right_start))
    return total


def capture_speaker_evidence(
    session: Session, file_id: str, segments: list[dict]
) -> None:
    """Backfill missing evidence before a prior diarized transcript is replaced."""
    timelines = _speaker_intervals(segments)
    if not timelines:
        return
    rows = {
        row.key: row
        for row in session.scalars(select(Speaker).where(Speaker.file_id == file_id))
    }
    for key, intervals in timelines.items():
        row = rows.get(key)
        if row is None:
            session.add(Speaker(file_id=file_id, key=key, timeline={"intervals": intervals}))
        elif not row.timeline:
            row.timeline = {"intervals": intervals}


def reconcile_speaker_labels(
    session: Session, file_id: str, segments: list[dict]
) -> dict[str, str]:
    """Map run-local diarization labels onto stable per-recording identities.

    Matches are one-to-one and require both substantial overlap and a clear margin
    over the runner-up. Ambiguous or new voices receive a fresh stable key, so an
    existing display name is never silently attached to uncertain speech.
    """
    current = _speaker_intervals(segments)
    if not current:
        return {}
    rows = list(session.scalars(select(Speaker).where(Speaker.file_id == file_id)))
    previous = {
        row.key: (row.timeline or {}).get("intervals", []) for row in rows if row.timeline
    }
    candidates: list[tuple[float, float, str, str]] = []
    for emitted, new_intervals in current.items():
        ranked: list[tuple[float, float, str]] = []
        for stable, old_intervals in previous.items():
            overlap = _overlap(new_intervals, old_intervals)
            denominator = _duration(new_intervals) + _duration(old_intervals)
            score = (2 * overlap / denominator) if denominator else 0.0
            ranked.append((score, overlap, stable))
        ranked.sort(reverse=True)
        if ranked:
            best_score, best_overlap, stable = ranked[0]
            runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
            if (
                best_overlap >= _MIN_OVERLAP_SECONDS
                and best_score >= _MIN_MATCH_SCORE
                and best_score - runner_up >= _AMBIGUITY_MARGIN
            ):
                candidates.append((best_score, best_overlap, emitted, stable))

    mapping: dict[str, str] = {}
    claimed: set[str] = set()
    for _score, _overlap_seconds, emitted, stable in sorted(candidates, reverse=True):
        if emitted not in mapping and stable not in claimed:
            mapping[emitted] = stable
            claimed.add(stable)

    used_keys = {row.key for row in rows}
    next_number = 0
    for emitted in current:
        if emitted in mapping:
            continue
        if emitted not in used_keys:
            stable = emitted
        else:
            while f"SPEAKER_{next_number:02d}" in used_keys:
                next_number += 1
            stable = f"SPEAKER_{next_number:02d}"
        mapping[emitted] = stable
        used_keys.add(stable)
        session.add(Speaker(file_id=file_id, key=stable))

    row_by_key = {row.key: row for row in rows}
    for emitted, stable in mapping.items():
        row = row_by_key.get(stable)
        if row is None:
            row = next(
                item
                for item in session.new
                if isinstance(item, Speaker)
                and item.file_id == file_id
                and item.key == stable
            )
        row.timeline = {"intervals": current[emitted]}
    for segment in segments:
        if segment.get("speaker") in mapping:
            segment["speaker"] = mapping[segment["speaker"]]
        for word in segment.get("words") or []:
            if word.get("speaker") in mapping:
                word["speaker"] = mapping[word["speaker"]]
    return mapping


def display_names(session: Session, file_id: str) -> dict[str, str]:
    """Map speaker key -> user display name, only for renamed speakers."""
    rows = session.scalars(select(Speaker).where(Speaker.file_id == file_id))
    return {row.key: row.display_name for row in rows if row.display_name}
