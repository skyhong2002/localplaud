"""Durable voiceprints and conservative, reversible speaker-name assignments.

Plaud labels are opt-in migration references only. Local diarization and new
recordings never depend on a Plaud transcript. Inferred names never enroll.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from .db.models import Base, Chunk, PlaudFile, Speaker
from .voice_matching import MODEL, REVISION, VERSION, clean_windows, match_voice, usable_name


def now():
    return datetime.now(UTC)


class VoiceSample(Base):
    __tablename__ = "voice_samples"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_id: Mapped[str] = mapped_column(String(64), index=True)
    speaker_key: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(32))
    reference_name: Mapped[str | None] = mapped_column(String(128), default=None)
    transcript_id: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))
    windows: Mapped[list] = mapped_column(JSON)
    vectors: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(256), default=MODEL)
    model_revision: Mapped[str] = mapped_column(String(64), default=REVISION)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class VoiceAssignment(Base):
    __tablename__ = "voice_assignments"
    speaker_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[str] = mapped_column(String(64), index=True)
    speaker_key: Mapped[str] = mapped_column(String(128))
    applied_name: Mapped[str | None] = mapped_column(String(128), default=None)
    previous_name: Mapped[str | None] = mapped_column(String(128), default=None)
    status: Mapped[str] = mapped_column(String(32))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class VoiceEvent(Base):
    __tablename__ = "voice_identity_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    speaker_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


def create_schema(engine):
    Base.metadata.create_all(
        engine, tables=[VoiceSample.__table__, VoiceAssignment.__table__, VoiceEvent.__table__]
    )


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def canonical(row):
    raw = row.local_transcript
    if raw is None:
        return None, []
    revision = row.corrected_transcript_for_source("local")
    return raw, revision.segments if revision is not None else raw.segments


def inventory(session, row, *, import_plaud=False):
    """Refresh active descriptors; obsolete samples remain audit data, not references."""
    raw, segments = canonical(row)
    sources = []
    if raw is not None:
        names = {}
        for speaker in row.speakers:
            assignment = session.get(VoiceAssignment, speaker.id)
            if (
                assignment
                and assignment.applied_name
                and speaker.display_name != assignment.applied_name
            ):
                if assignment.status != "overridden":
                    assignment.status = "overridden"
                    session.add(
                        VoiceEvent(
                            speaker_id=speaker.id,
                            action="manual_override",
                            detail={"previous_auto_name": assignment.applied_name},
                        )
                    )
            inferred = (
                assignment
                and assignment.status == "applied"
                and speaker.display_name == assignment.applied_name
            )
            if not inferred and usable_name(speaker.display_name):
                names[speaker.key] = speaker.display_name
        sources.append((raw, segments, "local", names))
    if import_plaud and row.plaud_transcript:
        cloud = row.plaud_transcript
        names = {
            s.get("speaker"): s.get("speaker")
            for s in cloud.segments
            if usable_name(s.get("speaker"))
        }
        sources.append((cloud, cloud.segments, "plaud-reference", names))
    active = []
    for transcript, values, source, names in sources:
        windows = clean_windows(values)
        fingerprint = digest(
            {
                "transcript": transcript.id,
                "windows": windows,
                "version": VERSION,
                "model": MODEL,
                "revision": REVISION,
            }
        )
        for key, ranges in windows.items():
            if source != "local" and key not in names:
                continue
            sid = digest([row.id, key, source, fingerprint])
            sample = session.get(VoiceSample, sid)
            if sample is None:
                sample = VoiceSample(
                    id=sid,
                    file_id=row.id,
                    speaker_key=key,
                    source=source,
                    transcript_id=transcript.id,
                    fingerprint=fingerprint,
                    windows=ranges,
                    reference_name=names.get(key),
                )
                session.add(sample)
            else:
                sample.reference_name = names.get(key)
            active.append(sample)
    session.flush()
    return active


def references(samples):
    return [
        {
            "name": s.reference_name,
            "file_id": s.file_id,
            "speaker_key": s.speaker_key,
            "vectors": s.vectors,
            "source": s.source,
            "sample_id": s.id,
        }
        for s in samples
        if s.status == "ready" and s.reference_name and len(s.vectors) >= 2
    ]


def propose(sample, refs, *, threshold=0.75, margin=0.12):
    return match_voice(
        sample.vectors, refs, threshold=threshold, margin=margin, query_file_id=sample.file_id
    )


def apply_match(session, sample, decision, *, threshold=0.75, margin=0.12):
    """Transactionally preserve claims, manual names, revisions, and provenance."""
    from .api.app import _queue_transcript_reindex, _serialize_transcript_mutation
    from .worker.pipeline import processing_claim_active

    _serialize_transcript_mutation(session, sample.file_id)
    row = session.get(PlaudFile, sample.file_id, populate_existing=True)
    if row is None or processing_claim_active(row):
        return "busy"
    # Refuse stale results following a diarization or segment ownership edit.
    active = inventory(session, row)
    if sample.id not in {s.id for s in active}:
        return "stale"
    speaker = session.scalar(
        select(Speaker).where(Speaker.file_id == row.id, Speaker.key == sample.speaker_key)
    )
    if speaker is None:
        return "missing_speaker"
    prior = session.get(VoiceAssignment, speaker.id)
    if prior and prior.status == "overridden":
        return "manual"
    if speaker.display_name:
        return (
            "already_applied"
            if prior and prior.status == "applied" and speaker.display_name == prior.applied_name
            else "manual"
        )
    evidence = {
        **decision,
        "sample_id": sample.id,
        "model": MODEL,
        "model_revision": REVISION,
        "version": VERSION,
        "threshold": threshold,
        "margin": margin,
        "reference_policy": "explicit-manual-or-opt-in-plaud; no-inferred-enrollment",
    }
    if prior is None:
        prior = VoiceAssignment(
            speaker_id=speaker.id,
            file_id=row.id,
            speaker_key=speaker.key,
            status=decision["status"],
            evidence=evidence,
        )
        session.add(prior)
    prior.status, prior.evidence = decision["status"], evidence
    if decision["status"] != "matched":
        return decision["status"]
    prior.previous_name = speaker.display_name
    prior.applied_name = decision["name"]
    prior.status = "applied"
    speaker.display_name = decision["name"]
    session.add(VoiceEvent(speaker_id=speaker.id, action="auto_assign", detail=evidence))
    session.execute(__import__("sqlalchemy").delete(Chunk).where(Chunk.file_id == row.id))
    _queue_transcript_reindex(session, row.id)
    return "applied"


def undo_assignment(session, speaker_id):
    """Undo only our unchanged assignment; keep later human edits and audio intact."""
    from .api.app import _queue_transcript_reindex, _serialize_transcript_mutation
    from .worker.pipeline import processing_claim_active

    prior = session.get(VoiceAssignment, speaker_id)
    speaker = session.get(Speaker, speaker_id)
    if not prior or not speaker:
        return False
    _serialize_transcript_mutation(session, speaker.file_id)
    row = session.get(PlaudFile, speaker.file_id, populate_existing=True)
    if processing_claim_active(row):
        return False
    if prior.status == "applied" and speaker.display_name == prior.applied_name:
        speaker.display_name = prior.previous_name
        prior.status = "overridden"
        session.add(
            VoiceEvent(speaker_id=speaker_id, action="undo", detail={"name": prior.applied_name})
        )
        session.execute(__import__("sqlalchemy").delete(Chunk).where(Chunk.file_id == row.id))
        _queue_transcript_reindex(session, row.id)
        return True
    return False
