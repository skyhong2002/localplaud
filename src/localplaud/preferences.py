"""Durable workspace display preferences."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from .db.models import KeyValue
from .db.tenancy import workspace_key
from .i18n import SUPPORTED_LOCALES

PREFERENCES_KEY = "workspace_preferences"
DEFAULT_WORKSPACE_PREFERENCES = {
    "workspace_name": "localplaud",
    "theme": "light",
    "density": "comfortable",
    "timezone": "Asia/Taipei",
    "hour_cycle": "24",
    "locale": "en",
    "auto_process_new_recordings": True,
}


def validate_timezone(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 64:
        raise ValueError("Timezone must be a valid IANA timezone")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Timezone must be a valid IANA timezone") from exc
    return value


def account_workspace_name(session: Session) -> str | None:
    """The owning account's username, which is always the workspace's name.

    ``None`` for a workspace without an account (accounts disabled), whose name
    stays a stored, editable preference.
    """
    from sqlalchemy import select

    from .db.models import AccountUser, Workspace
    from .db.tenancy import DEFAULT_WORKSPACE_ID, current_workspace_id

    workspace_id = current_workspace_id() or DEFAULT_WORKSPACE_ID
    return session.scalar(
        select(AccountUser.username)
        .join(Workspace, Workspace.owner_user_id == AccountUser.id)
        .where(Workspace.id == workspace_id)
    )


def get_workspace_preferences(session: Session) -> dict:
    row = session.get(KeyValue, workspace_key(PREFERENCES_KEY))
    stored = row.value if row and isinstance(row.value, dict) else {}
    preferences = DEFAULT_WORKSPACE_PREFERENCES | {
        key: stored[key] for key in DEFAULT_WORKSPACE_PREFERENCES if key in stored
    }
    preferences["theme"] = "light"
    if account_name := account_workspace_name(session):
        preferences["workspace_name"] = account_name
    preferences["workspace_name_locked"] = account_name is not None
    return preferences


def save_workspace_preferences(session: Session, values: dict) -> dict:
    preferences = DEFAULT_WORKSPACE_PREFERENCES | {
        key: values[key] for key in DEFAULT_WORKSPACE_PREFERENCES if key in values
    }
    preferences["theme"] = "light"
    if account_name := account_workspace_name(session):
        # An account's workspace is always named after the account.
        preferences["workspace_name"] = account_name
    preferences["timezone"] = validate_timezone(str(preferences["timezone"]))
    if preferences["locale"] not in SUPPORTED_LOCALES:
        raise ValueError("Interface language is not supported")
    key = workspace_key(PREFERENCES_KEY)
    row = session.get(KeyValue, key)
    if row is None:
        session.add(KeyValue(key=key, value=preferences))
    else:
        row.value = preferences
    session.flush()
    return get_workspace_preferences(session)
