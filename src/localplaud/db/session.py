"""Database engine/session setup and initialization."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings
from .models import Base

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
    from ..providers.service import bootstrap_default_profile
    from .migrations import (
        migrate_artifact_lineage_columns,
        migrate_import_schema,
        migrate_note_template_schema,
        migrate_organization_schema,
        migrate_profile_snapshot_columns,
    )

    migrate_profile_snapshot_columns(engine)
    migrate_organization_schema(engine)
    migrate_note_template_schema(engine)
    migrate_artifact_lineage_columns(engine)
    migrate_import_schema(engine)
    with Session(engine) as session:
        bootstrap_default_profile(session, get_settings())
        from ..worker.summary_templates import bootstrap_note_templates

        bootstrap_note_templates(session)
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
