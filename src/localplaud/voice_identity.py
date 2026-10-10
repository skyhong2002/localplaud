"""Durable voiceprints and conservative, reversible speaker-name assignments.

Plaud labels are opt-in migration references only. Local diarization and new
recordings never depend on a Plaud transcript. Inferred names never enroll.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text, select, text
from sqlalchemy.orm import Mapped, mapped_column

from .db.models import Base, Chunk, PlaudFile, Speaker
from .voice_matching import (
    MODEL,
    REVISION,
    VERSION,
    clean_windows,
    consistent_vectors,
    match_voice,
    usable_name,
)


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

    # Triggers cover permanent deletions by already-running older web processes.
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            connection.execute(
                text("""
                CREATE TRIGGER IF NOT EXISTS voice_identity_delete_speaker
                AFTER DELETE ON speakers BEGIN
                    DELETE FROM voice_identity_events WHERE speaker_id = OLD.id;
                    DELETE FROM voice_assignments WHERE speaker_id = OLD.id;
                END
            """)
            )
            connection.execute(
                text("""
                CREATE TRIGGER IF NOT EXISTS voice_identity_delete_recording
                AFTER DELETE ON plaud_files BEGIN
                    DELETE FROM voice_identity_events WHERE speaker_id IN
                        (SELECT speaker_id FROM voice_assignments WHERE file_id = OLD.id);
                    DELETE FROM voice_assignments WHERE file_id = OLD.id;
                    DELETE FROM voice_samples WHERE file_id = OLD.id;
                END
            """)
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


def validate_plaud_enrollment(enrollment):
    """Validate an explicit one-time enrollment snapshot; malformed data fails closed."""
    if enrollment is None:
        return
    if not isinstance(enrollment, dict) or enrollment.get("version") != 1:
        raise ValueError("invalid Plaud voice enrollment snapshot")
    names, samples = enrollment.get("names"), enrollment.get("samples")
    if (
        not isinstance(enrollment.get("id"), str)
        or not enrollment["id"]
        or enrollment.get("confirmed_manual") is not True
        or not isinstance(names, dict)
        or not isinstance(samples, dict)
    ):
        raise ValueError("Plaud enrollment requires confirmed names and frozen samples")
    minimum = enrollment.get("min_recordings")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
        raise ValueError("invalid enrollment recording minimum")
    if any(
        not usable_name(name)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count < minimum
        for name, count in names.items()
    ):
        raise ValueError("ineligible name in Plaud enrollment")
    if any(
        not isinstance(sid, str)
        or len(sid) != 64
        or any(c not in "0123456789abcdef" for c in sid)
        or name not in names
        for sid, name in samples.items()
    ):
        raise ValueError("invalid sample in Plaud enrollment")


def inventory(session, row, *, import_plaud=False, plaud_enrollment=None):
    """Refresh active descriptors; obsolete samples remain audit data, not references."""
    validate_plaud_enrollment(plaud_enrollment)
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
            if (
                source == "plaud-reference"
                and plaud_enrollment is not None
                and plaud_enrollment["samples"].get(sid) != names.get(key)
            ):
                continue
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


def validate_name_aliases(aliases):
    """Accept only explicit, one-hop identity merges; never infer aliases from case."""
    if aliases is None:
        return
    if not isinstance(aliases, dict) or any(
        not isinstance(source, str)
        or not isinstance(target, str)
        or not usable_name(source)
        or not usable_name(target)
        or source == target
        or target in aliases
        for source, target in aliases.items()
    ):
        raise ValueError("speaker name aliases must map explicit names directly to canonical names")


def references(samples, *, plaud_enrollment=None, name_aliases=None):
    validate_plaud_enrollment(plaud_enrollment)
    validate_name_aliases(name_aliases)
    name_aliases = name_aliases or {}
    return [
        {
            "name": name_aliases.get(s.reference_name, s.reference_name),
            "source_name": s.reference_name,
            "file_id": s.file_id,
            "speaker_key": s.speaker_key,
            "vectors": consistent_vectors(s.vectors),
            "source": s.source,
            "sample_id": s.id,
            "label_provenance": (
                "user-confirmed-manual"
                if s.source == "plaud-reference" and plaud_enrollment is not None
                else "plaud-import"
                if s.source == "plaud-reference"
                else "local-manual"
            ),
            "enrollment_id": plaud_enrollment["id"]
            if s.source == "plaud-reference" and plaud_enrollment is not None
            else None,
        }
        for s in samples
        if s.status == "ready"
        and s.reference_name
        and consistent_vectors(s.vectors)
        and (
            s.source != "plaud-reference"
            or plaud_enrollment is None
            or plaud_enrollment["samples"].get(s.id) == s.reference_name
        )
    ]


def propose(sample, refs, *, threshold=0.75, margin=0.12):
    return match_voice(
        sample.vectors, refs, threshold=threshold, margin=margin, query_file_id=sample.file_id
    )


def _prepare_note_name_change(session, file_id):
    """Fence every generated note before changing any name or assignment evidence."""
    from .db.models import Summary
    from .worker.knowledge_index import lock_summary_for_mutation

    previous_names = {
        row.key: row.display_name
        for row in session.scalars(select(Speaker).where(Speaker.file_id == file_id))
    }
    summary_ids = list(
        session.scalars(
            select(Summary.id)
            .where(Summary.file_id == file_id, Summary.source == "local")
            .order_by(Summary.id)
        )
    )
    for summary_id in summary_ids:
        lock_summary_for_mutation(session, summary_id, file_id)
    return previous_names


def apply_match(session, sample, decision, *, threshold=0.75, margin=0.12):
    """Transactionally preserve claims, manual names, revisions, and provenance."""
    from .api.app import _queue_transcript_reindex, _serialize_transcript_mutation
    from .note_speakers import refresh_generated_note_speaker_names
    from .worker.pipeline import processing_claim_active

    _serialize_transcript_mutation(session, sample.file_id)
    row = session.get(PlaudFile, sample.file_id, populate_existing=True)
    if row is None or row.is_trash:
        return "removed"
    if processing_claim_active(row):
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
    previous_names = (
        _prepare_note_name_change(session, row.id) if decision["status"] == "matched" else None
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
    session.flush()
    refresh_generated_note_speaker_names(session, row.id, previous_names=previous_names)
    session.add(VoiceEvent(speaker_id=speaker.id, action="auto_assign", detail=evidence))
    session.execute(__import__("sqlalchemy").delete(Chunk).where(Chunk.file_id == row.id))
    _queue_transcript_reindex(session, row.id, names_only=True)
    return "applied"


def undo_assignment(session, speaker_id):
    """Undo only our unchanged assignment; keep later human edits and audio intact."""
    from .api.app import _queue_transcript_reindex, _serialize_transcript_mutation
    from .note_speakers import refresh_generated_note_speaker_names
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
        previous_names = _prepare_note_name_change(session, row.id)
        speaker.display_name = prior.previous_name
        prior.status = "overridden"
        session.flush()
        refresh_generated_note_speaker_names(session, row.id, previous_names=previous_names)
        session.add(
            VoiceEvent(speaker_id=speaker_id, action="undo", detail={"name": prior.applied_name})
        )
        session.execute(__import__("sqlalchemy").delete(Chunk).where(Chunk.file_id == row.id))
        _queue_transcript_reindex(session, row.id, names_only=True)
        return True
    return False
