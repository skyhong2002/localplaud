"""Workspace lifecycle: one private library per account."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db.models import AccountUser, Workspace
from .db.tenancy import DEFAULT_WORKSPACE_ID, system_scope, workspace_scope


def prepare_workspace(session: Session, *, reconcile_index: bool = True) -> None:
    """Seed and reconcile the active workspace's local defaults.

    Idempotent: runs at startup for every workspace and once when a workspace
    is created. Built-in note templates are versioned copies per workspace so
    personal edits and installs never cross accounts. ``reconcile_index=False``
    leaves the (slow, library-wide) note index reconcile to a background job.
    """
    from .automations import ensure_default_note_templates
    from .worker.knowledge_index import sync_knowledge_documents
    from .worker.summary_templates import bootstrap_note_templates

    bootstrap_note_templates(session)
    ensure_default_note_templates(session)
    if not reconcile_index:
        return
    # Discover current note artifacts without doing any provider work.
    # Embedding remains an explicit mutation/worker action, so a serve-only
    # process or a restart with automatic processing disabled stays idle.
    sync_knowledge_documents(session, get_settings())


def workspace_for_user(session: Session, user_id: int) -> int | None:
    with system_scope():
        return session.scalar(select(Workspace.id).where(Workspace.owner_user_id == user_id))


def ensure_user_workspace(session: Session, user: AccountUser) -> int:
    """Return the user's workspace, creating and seeding it on first approval.

    The owner keeps workspace 1, which holds the library that predates accounts.
    """
    existing = workspace_for_user(session, user.id)
    if existing is not None:
        return existing
    with system_scope():
        if user.role == "owner":
            default = session.get(Workspace, 1)
            if default is not None and default.owner_user_id is None:
                default.owner_user_id = user.id
                session.flush()
                return default.id
        workspace = Workspace(name=user.username[:80], owner_user_id=user.id)
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
    from .preferences import get_workspace_preferences, save_workspace_preferences

    # Start from the original workspace's regional settings, under the new name.
    with workspace_scope(DEFAULT_WORKSPACE_ID):
        inherited = get_workspace_preferences(session)
    with workspace_scope(workspace_id):
        save_workspace_preferences(
            session,
            {key: inherited[key] for key in ("timezone", "hour_cycle", "locale", "density")}
            | {"workspace_name": user.username[:80]},
        )
        prepare_workspace(session)
        session.flush()
    return workspace_id


def all_workspace_ids(session: Session) -> list[int]:
    with system_scope():
        return list(session.scalars(select(Workspace.id).order_by(Workspace.id)))
