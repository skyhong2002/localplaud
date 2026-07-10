"""Small idempotent data migrations that do not need an external migration tool."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import Chunk, FileStatus, KeyValue, PlaudFile, Transcript

INDEPENDENT_MIGRATION_KEY = "migration.independent-artifacts.v1"
_PLAUD_SOURCES = {"cloud", "plaud"}


def _legacy_template(template: str, used: set[str], row_id: int) -> str:
    """Return a unique <=64-char template name for preserved legacy notes."""
    prefix = "legacy-cloud-"
    candidate = f"{prefix}{template}"[:64]
    if candidate not in used:
        return candidate
    suffix = f"-{row_id}"
    return f"{candidate[: 64 - len(suffix)]}{suffix}"


def prepare_independent_mode(engine: Engine, *, force: bool = False) -> dict[str, int]:
    """Make legacy cloud-derived rows safe for raw-audio-only processing.

    Plaud transcripts are preserved alongside future local transcripts. Files that
    have only a Plaud transcript are requeued when their audio still exists. Local
    summaries made from those transcripts are retained but relabelled as legacy so
    they cannot satisfy a local summary stage; their non-provenanced chunks are
    discarded for regeneration from the future canonical local transcript.

    The marker keeps normal startup cheap and prevents repeatedly retrying genuine
    pipeline errors. Importing another cloud transcript clears the marker.
    """
    counts = {"files": 0, "summaries": 0, "chunks": 0, "requeued": 0}
    with Session(engine) as session:
        marker = session.get(KeyValue, INDEPENDENT_MIGRATION_KEY)
        if marker is not None and not force:
            return counts

        cloud_file_ids = set(
            session.scalars(select(Transcript.file_id).where(Transcript.source.in_(_PLAUD_SOURCES)))
        )
        local_file_ids = set(
            session.scalars(select(Transcript.file_id).where(Transcript.source == "local"))
        )
        affected = cloud_file_ids - local_file_ids

        for file_id in affected:
            file = session.get(PlaudFile, file_id)
            if file is None:
                continue
            counts["files"] += 1
            used_templates = {summary.template for summary in file.summaries}
            for summary in file.summaries:
                if summary.source != "local":
                    continue
                renamed = _legacy_template(summary.template, used_templates, summary.id)
                used_templates.add(renamed)
                summary.template = renamed
                summary.source = "legacy"
                counts["summaries"] += 1

            deleted = session.execute(delete(Chunk).where(Chunk.file_id == file_id)).rowcount
            counts["chunks"] += int(deleted or 0)

            if file.audio_path and Path(file.audio_path).exists():
                file.status = FileStatus.downloaded
                file.error = None
                counts["requeued"] += 1

        if marker is None:
            session.add(KeyValue(key=INDEPENDENT_MIGRATION_KEY, value=counts.copy()))
        else:
            marker.value = counts.copy()
        session.commit()
    return counts
