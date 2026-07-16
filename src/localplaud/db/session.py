"""Database engine/session setup and initialization."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from ..error_redaction import sanitize_error_value
from .models import Base

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None
_schema_thread_lock = threading.Lock()


@event.listens_for(Session, "before_flush")
def _sanitize_durable_diagnostics(session: Session, _flush_context, _instances) -> None:
    """Redact diagnostic fields immediately before ORM persistence."""
    for instance in session.new.union(session.dirty):
        for attribute in ("error", "health", "detail", "response_excerpt"):
            if not hasattr(instance, attribute):
                continue
            value = getattr(instance, attribute)
            sanitized = sanitize_error_value(value)
            if sanitized != value:
                setattr(instance, attribute, sanitized)


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


@contextmanager
def _schema_initialization_lock(engine: Engine):
    """Serialize check-then-create migrations across processes."""
    dialect = engine.dialect.name
    if dialect == "sqlite" and engine.url.database not in {None, ":memory:"}:
        try:
            import fcntl
        except ImportError:
            with _schema_thread_lock:
                yield
            return
        database_path = Path(str(engine.url.database)).resolve()
        lock_path = database_path.with_name(f"{database_path.name}.schema.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if dialect == "postgresql":
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT pg_advisory_lock(1280330575)")
            try:
                yield
            finally:
                connection.exec_driver_sql("SELECT pg_advisory_unlock(1280330575)")
        return
    with _schema_thread_lock:
        yield


def init_db() -> dict[str, int] | None:
    """Create tables and prepare legacy cloud-derived rows when required."""
    engine = get_engine()
    with _schema_initialization_lock(engine):
        return _init_db_locked(engine)


def _init_db_locked(engine: Engine) -> dict[str, int] | None:
    Base.metadata.create_all(engine)
    from ..providers.service import bootstrap_default_profile
    from .migrations import (
        migrate_artifact_lineage_columns,
        migrate_ask_provenance_schema,
        migrate_ask_request_claim_schema,
        migrate_automation_ownership_schema,
        migrate_editable_note_provenance_schema,
        migrate_editable_note_revision_schema,
        migrate_editable_note_source_schema,
        migrate_import_schema,
        migrate_knowledge_index_schema,
        migrate_legacy_note_template_schema,
        migrate_legacy_provider_profile_schema,
        migrate_legacy_stage_run_schema,
        migrate_legacy_summary_schema,
        migrate_local_transcript_uniqueness,
        migrate_note_template_schema,
        migrate_organization_schema,
        migrate_pipeline_retry_schema,
        migrate_processing_claim_schema,
        migrate_profile_resolution_schema,
        migrate_profile_snapshot_columns,
        migrate_speaker_timeline_schema,
        migrate_stage_attempt_schema,
        migrate_summary_revision_schema,
        migrate_transcript_revision_provenance,
        migrate_vocabulary_schema,
        redact_legacy_error_text,
    )

    migrate_legacy_provider_profile_schema(engine)
    migrate_legacy_note_template_schema(engine)
    migrate_legacy_summary_schema(engine)
    migrate_legacy_stage_run_schema(engine)
    migrate_local_transcript_uniqueness(engine)
    migrate_profile_snapshot_columns(engine)
    migrate_automation_ownership_schema(engine)
    migrate_stage_attempt_schema(engine)
    migrate_transcript_revision_provenance(engine)
    migrate_organization_schema(engine)
    migrate_pipeline_retry_schema(engine)
    migrate_processing_claim_schema(engine)
    migrate_profile_resolution_schema(engine)
    migrate_note_template_schema(engine)
    migrate_artifact_lineage_columns(engine)
    migrate_summary_revision_schema(engine)
    migrate_ask_provenance_schema(engine)
    migrate_ask_request_claim_schema(engine)
    migrate_editable_note_source_schema(engine)
    migrate_editable_note_revision_schema(engine)
    migrate_editable_note_provenance_schema(engine)
    migrate_speaker_timeline_schema(engine)
    migrate_import_schema(engine)
    migrate_knowledge_index_schema(engine)
    migrate_vocabulary_schema(engine)
    redact_legacy_error_text(engine)
    with Session(engine) as session:
        bootstrap_default_profile(session, get_settings())
        from ..worker.summary_templates import bootstrap_note_templates

        bootstrap_note_templates(session)
        # Discover current note artifacts without doing any provider work.
        # Embedding remains an explicit mutation/worker action, so a serve-only
        # process or a restart with automatic processing disabled stays idle.
        from ..worker.knowledge_index import sync_knowledge_documents

        sync_knowledge_documents(session, get_settings())
        session.commit()
    if get_settings().pipeline.artifact_mode == "independent":
        from .migrations import prepare_independent_mode

        return prepare_independent_mode(engine)
    return None


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
