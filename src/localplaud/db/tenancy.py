"""Workspace isolation for every ORM read and write.

Each account owns one private workspace. Isolation is enforced here, once, rather
than in each query:

- every ORM ``SELECT``/``UPDATE``/``DELETE`` touching a ``WorkspaceOwned`` model
  is limited to the active workspace (``with_loader_criteria``), including
  relationship loads and ``Session.get``;
- every new ``WorkspaceOwned`` row is stamped with the active workspace, and a
  row stamped for another workspace is refused at flush.

The active workspace lives in a context variable. Code that never sets one runs
in the original workspace (``DEFAULT_WORKSPACE_ID``): right for the daemon and
CLI, which predate workspaces, and wrong for work a member's request starts.
New threads begin unbound, so every spawn must bind the caller's scope
(:func:`run_in_current_workspace`), adopt a recording's workspace
(:func:`scoped_to_file`), or be workspace-free; ``tests/test_workspace_threads``
enforces this. System loops that must see every workspace use
:func:`system_scope` and enter :func:`workspace_scope` per workspace before
doing work. Never reuse one ``Session`` across scopes: its identity map would
return objects loaded under the previous scope.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from .models import PlaudFile, WorkspaceOwned

DEFAULT_WORKSPACE_ID = 1
_SYSTEM = object()
# Unbound code reads and writes the original workspace, but unlike an explicit
# scope it may adopt a recording's workspace (see ``scoped_to_file``).
_UNBOUND = object()
_active: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "localplaud_workspace", default=_UNBOUND
)


class WorkspaceViolation(RuntimeError):
    """A write would place a row in a workspace other than the active one."""


def current_workspace_id() -> int | None:
    """Active workspace id, or ``None`` inside :func:`system_scope`."""
    value = _active.get()
    if value is _UNBOUND:
        return DEFAULT_WORKSPACE_ID
    return None if value is _SYSTEM else value


def scope_is_bound() -> bool:
    return _active.get() is not _UNBOUND


@contextmanager
def workspace_scope(workspace_id: int) -> Iterator[int]:
    token = _active.set(int(workspace_id))
    try:
        yield int(workspace_id)
    finally:
        _active.reset(token)


@contextmanager
def system_scope() -> Iterator[None]:
    """Unfiltered access for cross-workspace scheduling and administration."""
    token = _active.set(_SYSTEM)
    try:
        yield
    finally:
        _active.reset(token)


def run_in_current_workspace(target: Callable[..., Any]) -> Callable[..., Any]:
    """Bind ``target`` to the caller's context so a new thread keeps its scope."""
    context = contextvars.copy_context()

    def bound(*args: Any, **kwargs: Any) -> Any:
        return context.run(target, *args, **kwargs)

    return bound


HOST_PRIVILEGE_ERROR = (
    "host secrets (env:) and private-network destinations are reserved for the owner's workspace"
)


def host_privileges_allowed() -> bool:
    """Whether the active workspace may use host environment secrets and reach
    private networks.

    Environment variables and the local network belong to the server operator,
    whose library is the original workspace. Members configure public endpoints
    without host credentials.
    """
    return current_workspace_id() in (None, DEFAULT_WORKSPACE_ID)


def workspace_key(key: str) -> str:
    """Per-workspace name for a ``kv`` row; the original workspace keeps bare keys."""
    workspace_id = current_workspace_id()
    if workspace_id is None:
        raise WorkspaceViolation(f"kv key {key!r} needs a workspace")
    return key if workspace_id == DEFAULT_WORKSPACE_ID else f"ws{workspace_id}:{key}"


def scoped_to_file(function: Callable[..., Any]) -> Callable[..., Any]:
    """Run ``function(file_id, ...)`` inside the recording's own workspace.

    Pipeline entry points are called from the daemon (unbound or system scope)
    and from request threads. An explicit scope for a different workspace is a
    bug and is refused rather than silently crossing workspaces.
    """
    import functools

    @functools.wraps(function)
    def wrapper(file_id: str, *args: Any, **kwargs: Any) -> Any:
        from .session import session_scope

        with session_scope() as session:
            owner = workspace_of_file(session, file_id)
        active = _active.get()
        if owner is None:
            return function(file_id, *args, **kwargs)
        if active not in (_UNBOUND, _SYSTEM) and active != owner:
            raise WorkspaceViolation(f"recording is not in workspace {active}")
        with workspace_scope(owner):
            return function(file_id, *args, **kwargs)

    wrapper.scoped_to_recording = True
    return wrapper


def local_file_id(workspace_id: int, plaud_id: str) -> str:
    """Local primary key for a Plaud file in ``workspace_id``.

    The original workspace keeps Plaud's id unchanged, so existing audio paths,
    share links and citations stay valid.
    """
    if int(workspace_id) == DEFAULT_WORKSPACE_ID:
        return plaud_id
    return f"w{int(workspace_id)}-{plaud_id}"


def plaud_file_id(workspace_id: int, file_id: str) -> str:
    """Inverse of :func:`local_file_id` for a recording in ``workspace_id``."""
    if int(workspace_id) == DEFAULT_WORKSPACE_ID:
        return file_id
    prefix = f"w{int(workspace_id)}-"
    if not file_id.startswith(prefix):
        raise WorkspaceViolation(f"recording is not in workspace {workspace_id}")
    return file_id[len(prefix) :]


def workspace_of_file(session: Session, file_id: str) -> int | None:
    with system_scope():
        return session.scalar(select(PlaudFile.workspace_id).where(PlaudFile.id == file_id))


@event.listens_for(Session, "do_orm_execute")
def _limit_to_workspace(state: ORMExecuteState) -> None:
    workspace_id = current_workspace_id()
    if workspace_id is None or state.is_column_load:
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    state.statement = state.statement.options(
        with_loader_criteria(
            WorkspaceOwned,
            lambda cls: cls.workspace_id == workspace_id,
            include_aliases=True,
        )
    )


def _parent_workspace(session: Session, instance: Any) -> int | None:
    relationships = type(instance).__mapper__.relationships
    file = getattr(instance, "file", None) if "file" in relationships else None
    if file is not None and file.workspace_id is not None:
        return file.workspace_id
    file_id = getattr(instance, "file_id", None)
    if file_id:
        with session.no_autoflush:
            return workspace_of_file(session, file_id)
    return None


@event.listens_for(Session, "before_flush")
def _stamp_workspace(session: Session, _flush_context, _instances) -> None:
    active = current_workspace_id()
    for instance in session.new:
        if not isinstance(instance, WorkspaceOwned):
            continue
        if instance.workspace_id is None:
            instance.workspace_id = (
                active if active is not None else _parent_workspace(session, instance)
            )
        if instance.workspace_id is None:
            raise WorkspaceViolation(
                f"{type(instance).__name__} has no workspace; run it inside workspace_scope()"
            )
        if active is not None and instance.workspace_id != active:
            raise WorkspaceViolation(
                f"{type(instance).__name__} belongs to workspace {instance.workspace_id}, "
                f"not the active workspace {active}"
            )
    if active is None:
        return
    for instance in session.dirty:
        if isinstance(instance, WorkspaceOwned) and instance.workspace_id != active:
            raise WorkspaceViolation(f"{type(instance).__name__} moved out of workspace {active}")
