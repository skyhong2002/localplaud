"""Durable version history for generated notes (Summary rows).

Every displacement of a live generated Summary — regeneration or an explicit
restore — first preserves the outgoing version as an immutable
``SummaryRevision``. History is scoped per (file, template): each note output
and the mind map keep independent chains. Only locally generated rows are
archived; imported Plaud mirrors (``source != "local"``) are refreshed copies
of cloud state, not localplaud-generated versions.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db.models import StageName, StageRun, StageStatus, Summary, SummaryRevision

# Attributes copied verbatim between the live row and an archived version.
SNAPSHOT_FIELDS: tuple[str, ...] = (
    "title",
    "content_md",
    "llm_provider",
    "model",
    "source",
    "template_version",
    "template_snapshot",
    "input_transcript_id",
    "input_transcript_revision",
    "input_transcript_source",
)


def content_fingerprint(values: Any) -> str:
    """Canonical content+provenance identity used to skip duplicate versions."""

    def read(field: str) -> Any:
        if isinstance(values, dict):
            return values.get(field)
        return getattr(values, field, None)

    payload = {field: read(field) for field in (*SNAPSHOT_FIELDS, "template")}
    payload["resolved_profile_snapshot"] = read("resolved_profile_snapshot") or {}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint_digest(values: Any) -> str:
    """Compact stored form of the content fingerprint (the full form is the
    serialized content itself, too large to persist as a reference)."""
    return hashlib.sha256(content_fingerprint(values).encode()).hexdigest()


def source_summary_provenance(row: Summary | SummaryRevision) -> dict:
    """Immutable record of exactly which generated content a copy came from.

    ``UserNote.source_summary_id`` points at the live output slot, and a
    history restore rewrites that slot in place — so the id alone cannot say
    which version the copy was made from. This snapshot pins it.
    """
    return {
        "template": row.template,
        "template_version": row.template_version,
        "llm_provider": row.llm_provider,
        "model": row.model,
        "source": row.source,
        "input_transcript_id": row.input_transcript_id,
        "input_transcript_revision": row.input_transcript_revision,
        "input_transcript_source": row.input_transcript_source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "content_fingerprint": fingerprint_digest(row),
    }


def latest_revision_number(session: Session, file_id: str, template: str) -> int:
    return (
        session.scalar(
            select(func.max(SummaryRevision.revision)).where(
                SummaryRevision.file_id == file_id,
                SummaryRevision.template == template,
            )
        )
        or 0
    )


def archive_summary(
    session: Session,
    row: Summary,
    *,
    reason: str,
    replacement_fingerprint: str | None = None,
) -> SummaryRevision | None:
    """Preserve ``row`` as the next immutable version of its template chain.

    Returns None without writing when the content is already preserved: the
    latest archived version is identical, or the incoming replacement is
    byte-for-byte the same (the content simply stays live).
    """
    if row.source != "local":
        return None
    fingerprint = content_fingerprint(row)
    if replacement_fingerprint is not None and fingerprint == replacement_fingerprint:
        return None
    latest = session.scalar(
        select(SummaryRevision)
        .where(
            SummaryRevision.file_id == row.file_id,
            SummaryRevision.template == row.template,
        )
        .order_by(SummaryRevision.revision.desc())
        .limit(1)
    )
    if latest is not None and content_fingerprint(latest) == fingerprint:
        return None
    archived = SummaryRevision(
        file_id=row.file_id,
        template=row.template,
        revision=(latest.revision if latest is not None else 0) + 1,
        created_at=row.created_at,
        archive_reason=reason,
        **{field: getattr(row, field) for field in SNAPSHOT_FIELDS},
        resolved_profile_snapshot=row.resolved_profile_snapshot or {},
    )
    session.add(archived)
    return archived


def restore_summary_version(
    session: Session, row: Summary, target: SummaryRevision
) -> bool:
    """Make ``target`` the live content of ``row``, preserving what it displaces.

    The displaced current version is archived first, then the live row is
    updated in place — its identity (and links such as editable-copy
    provenance) survives. The caller invalidates and requeues this note's
    independent knowledge document; transcript chunks remain untouched. The
    mind map is generated from the transcript *plus* its source note output,
    so a mind map sourced from the restored template is marked out of date
    (stale, exactly like a vocabulary or template change) rather than being
    presented as current. Returns True when that marking happened.
    """
    archive_summary(
        session,
        row,
        reason="restore",
        replacement_fingerprint=content_fingerprint(target),
    )
    for field in SNAPSHOT_FIELDS:
        setattr(row, field, getattr(target, field))
    row.resolved_profile_snapshot = target.resolved_profile_snapshot or {}
    # Keep the original generation time of the restored content truthful.
    # Legacy pre-archival revisions may carry no creation time; fall back to
    # the closest truthful bound instead of writing NULL into the live row.
    row.created_at = target.created_at or target.archived_at or row.created_at
    row.restored_from_revision = target.revision
    return _mark_dependent_mind_map_stale(session, row)


def _mark_dependent_mind_map_stale(session: Session, restored: Summary) -> bool:
    """Mark the mind map out of date when its source note version changed.

    The pipeline generates the mind map from the transcript plus the live
    content of one note template (recorded in ``template_snapshot`` as
    ``source_template_key``). Restoring a version of that template changes
    the mind map's input, so it can no longer be presented as current. A
    legacy mind map without a recorded source cannot be proven current
    either, so it is marked as well — unless its recorded ``source_note``
    fingerprint proves the restored content is exactly what it was built
    from. Restoring the mind map itself has no downstream consumer. Marking
    uses the established stale idiom — the artifact row is preserved and
    nothing is queued.
    """
    if restored.template == "mind_map":
        return False
    live_map = session.scalar(
        select(Summary).where(
            Summary.file_id == restored.file_id,
            Summary.template == "mind_map",
            Summary.source == "local",
        )
    )
    if live_map is None:
        return False
    source_key = (live_map.template_snapshot or {}).get("source_template_key")
    if source_key is not None and source_key != restored.template:
        return False
    # A map that records exactly which note content it was built from stays
    # current when that same content comes back — restoring the very version
    # the map used is a no-op for its input.
    recorded_fingerprint = ((live_map.template_snapshot or {}).get("source_note") or {}).get(
        "content_fingerprint"
    )
    if recorded_fingerprint is not None and recorded_fingerprint == fingerprint_digest(restored):
        return False
    run = session.scalar(
        select(StageRun).where(
            StageRun.file_id == restored.file_id,
            StageRun.stage == StageName.mind_map,
        )
    )
    if run is None:
        run = StageRun(
            file_id=restored.file_id, stage=StageName.mind_map, attempts=0, detail={}
        )
        session.add(run)
    run.status = StageStatus.pending
    run.error = None
    run.completed_at = None
    run.detail = dict(run.detail or {}) | {
        "stale": True,
        "stale_generation": secrets.token_hex(16),
        "reason": "note version restored",
    }
    return True
