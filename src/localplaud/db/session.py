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


def init_db() -> None:
    """Create tables if they don't exist."""
    Base.metadata.create_all(get_engine())


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
