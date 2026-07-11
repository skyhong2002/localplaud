"""Small idempotent data migrations that do not need an external migration tool."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import Chunk, FileStatus, KeyValue, PlaudFile, Transcript

INDEPENDENT_MIGRATION_KEY = "migration.independent-artifacts.v1"
_PLAUD_SOURCES = {"cloud", "plaud"}


def migrate_organization_schema(engine: Engine) -> list[str]:
    """Add local folder/tag metadata to an existing SQLite library."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    migrated: list[str] = []
    with engine.begin() as connection:
        if "plaud_files" in existing:
            columns = {column["name"] for column in inspector.get_columns("plaud_files")}
            if "folder_id" not in columns:
                connection.execute(
                    text("ALTER TABLE plaud_files ADD COLUMN folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL")
                )
                migrated.append("plaud_files.folder_id")
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(80) NOT NULL,
                color VARCHAR(64),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(80) NOT NULL,
                color VARCHAR(64),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS recording_tags (
                file_id VARCHAR(64) NOT NULL REFERENCES plaud_files(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (file_id, tag_id)
            )
        """))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_plaud_files_folder_id ON plaud_files (folder_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_tags_tag_id ON recording_tags (tag_id)"))
    for table in ("folders", "tags", "recording_tags"):
        if table not in existing:
            migrated.append(table)
    return migrated


def migrate_note_template_schema(engine: Engine) -> list[str]:
    """Add editable-note-template metadata to an existing SQLite library."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    migrated: list[str] = []
    with engine.begin() as connection:
        for table, column, ddl in (
            ("plaud_files", "note_template_key", "VARCHAR(64)"),
            ("summaries", "template_version", "INTEGER"),
            ("summaries", "template_snapshot", "JSON"),
            ("note_templates", "category", "VARCHAR(80)"),
            ("note_templates", "scenario", "VARCHAR(80)"),
            ("note_templates", "description", "VARCHAR(512)"),
            ("note_templates", "author", "VARCHAR(120)"),
            ("note_templates", "provenance", "VARCHAR(32)"),
            ("note_templates", "popularity", "INTEGER"),
        ):
            if table not in tables:
                continue
            columns = {item["name"] for item in inspector.get_columns(table)}
            if column not in columns:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                migrated.append(f"{table}.{column}")
    return migrated


def migrate_artifact_lineage_columns(engine: Engine) -> list[str]:
    """Add canonical transcript lineage to derived artifacts."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    migrated: list[str] = []
    with engine.begin() as connection:
        for table in ("summaries", "chunks"):
            if table not in tables:
                continue
            columns = {item["name"] for item in inspector.get_columns(table)}
            for column, ddl in (
                ("input_transcript_id", "INTEGER"),
                ("input_transcript_revision", "INTEGER"),
                ("input_transcript_source", "VARCHAR(16)"),
            ):
                if column not in columns:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    )
                    migrated.append(f"{table}.{column}")
    return migrated


def migrate_import_schema(engine: Engine) -> list[str]:
    """Add recording origin to existing SQLite libraries."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "plaud_files" not in inspector.get_table_names():
        return []
    columns = {item["name"] for item in inspector.get_columns("plaud_files")}
    if "origin" in columns:
        return []
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE plaud_files ADD COLUMN origin "
                "VARCHAR(32) NOT NULL DEFAULT 'plaud'"
            )
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_plaud_files_origin ON plaud_files (origin)")
        )
    return ["plaud_files.origin"]


def migrate_profile_snapshot_columns(engine: Engine) -> list[str]:
    """Add immutable profile provenance to existing SQLite artifact tables."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    migrated: list[str] = []
    for table in ("stage_runs", "transcripts", "summaries", "chunks"):
        if table not in existing:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "resolved_profile_snapshot" in columns:
            continue
        with engine.begin() as connection:
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN resolved_profile_snapshot JSON")
            )
        migrated.append(table)
    return migrated


def migrate_stage_run_snapshot_column(engine: Engine) -> bool:
    """Backward-compatible wrapper for the original single-column migration."""
    return "stage_runs" in migrate_profile_snapshot_columns(engine)


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
