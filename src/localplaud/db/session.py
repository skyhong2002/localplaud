"""Database engine/session setup and initialization."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from .models import Base, Summary

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def _ensure_sqlite_dir(url: str) -> None:
    prefix = "sqlite:///"
    if url.startswith(prefix):
        path = Path(url[len(prefix) :])
        path.parent.mkdir(parents=True, exist_ok=True)


def get_engine() -> Engine:
    global _engine, _Session
    if _engine is None:
        url = get_settings().store.database_url
        _ensure_sqlite_dir(url)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
        _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db() -> dict[str, int] | None:
    """Create tables and prepare legacy cloud-derived rows when required."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    _ensure_runtime_columns(engine)
    _ensure_embedding_spaces(engine)
    _ensure_summary_source_constraint(engine)
    from ..providers.bootstrap import bootstrap_legacy_profile

    with Session(engine) as session:
        bootstrap_legacy_profile(session, get_settings())
        from ..worker.summary_templates import bootstrap_templates

        bootstrap_templates(session)
        session.commit()
    if get_settings().pipeline.artifact_mode == "independent":
        from .migrations import prepare_independent_mode

        return prepare_independent_mode(engine)
    return None


def _ensure_runtime_columns(engine: Engine) -> None:
    """Idempotently add columns create_all cannot add to an existing SQLite DB."""
    if engine.dialect.name != "sqlite":
        return
    schema = inspect(engine)
    additions = {
        "plaud_files": {"execution_profile_id": "INTEGER", "retry_count": "INTEGER NOT NULL DEFAULT 0",
                        "retry_after": "DATETIME", "profile_context": "JSON NOT NULL DEFAULT '{}'",
                        "processing_token": "VARCHAR(64)", "processing_lease_until": "DATETIME"},
        "stage_runs": {"profile_snapshot": "JSON NOT NULL DEFAULT '{}'",
                       "latency_ms": "BIGINT", "usage": "JSON NOT NULL DEFAULT '{}'",
                       "estimated_cost": "FLOAT", "actual_cost": "FLOAT"},
        "transcript_revisions": {"kind": "VARCHAR(32) NOT NULL DEFAULT 'user'"},
        "transcripts": {"profile_snapshot": "JSON NOT NULL DEFAULT '{}'",
                        "artifact_kind": "VARCHAR(32) NOT NULL DEFAULT 'canonical'"},
        "summaries": {
            "revision": "INTEGER NOT NULL DEFAULT 1",
            "template_version": "INTEGER",
            "transcript_revision": "INTEGER",
            "profile_snapshot": "JSON NOT NULL DEFAULT '{}'",
        },
        "chunks": {"profile_snapshot": "JSON NOT NULL DEFAULT '{}'",
                   "embedding_provider": "VARCHAR(64)",
                   "transcript_revision": "INTEGER"},
        "note_templates": {"execution_profile_id": "INTEGER"},
        "ask_messages": {"usage": "JSON NOT NULL DEFAULT '{}'",
                         "estimated_cost": "FLOAT", "actual_cost": "FLOAT"},
    }
    with engine.begin() as connection:
        for table, columns in additions.items():
            existing = {column["name"] for column in schema.get_columns(table)}
            for name, declaration in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"))


def _ensure_summary_source_constraint(engine: Engine) -> None:
    """Rebuild legacy SQLite summaries uniqueness to include provenance source."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        indexes = connection.execute(text("PRAGMA index_list('summaries')")).mappings().all()
        unique_columns = []
        for index in indexes:
            if not index["unique"]:
                continue
            escaped = str(index["name"]).replace("'", "''")
            columns = connection.execute(
                text(f"PRAGMA index_info('{escaped}')")
            ).mappings().all()
            unique_columns.append([column["name"] for column in columns])
        if ["file_id", "template", "source"] in unique_columns:
            return
        if ["file_id", "template"] not in unique_columns:
            return
        connection.execute(text("ALTER TABLE summaries RENAME TO summaries_legacy_p0"))
        Summary.__table__.create(connection)
        columns = [column.name for column in Summary.__table__.columns]
        names = ", ".join(f'"{name}"' for name in columns)
        connection.execute(text(
            f"INSERT INTO summaries ({names}) SELECT {names} FROM summaries_legacy_p0"
        ))
        connection.execute(text("DROP TABLE summaries_legacy_p0"))


def _ensure_embedding_spaces(engine: Engine) -> None:
    """Upgrade pre-P0 model labels to a comparable embedding-space identity."""
    from ..embeddings.base import build_embedder

    settings = get_settings()
    embedder = build_embedder(settings.embeddings)
    space_id = getattr(embedder, "space_id", embedder.name)
    if space_id == embedder.name:
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE chunks SET embedding_model = :space, embedding_provider = :provider "
                "WHERE embedding_model IN (:legacy, :provider) "
                "AND embedding_provider IS NULL"
            ),
            {"space": space_id, "provider": settings.embeddings.provider,
             "legacy": embedder.name},
        )


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context."""
    if _Session is None:
        get_engine()
    assert _Session is not None
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
