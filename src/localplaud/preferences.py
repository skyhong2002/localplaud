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


def get_workspace_preferences(session: Session) -> dict:
    row = session.get(KeyValue, workspace_key(PREFERENCES_KEY))
    stored = row.value if row and isinstance(row.value, dict) else {}
    preferences = DEFAULT_WORKSPACE_PREFERENCES | {
        key: stored[key] for key in DEFAULT_WORKSPACE_PREFERENCES if key in stored
    }
    preferences["theme"] = "light"
    return preferences


def save_workspace_preferences(session: Session, values: dict) -> dict:
    preferences = DEFAULT_WORKSPACE_PREFERENCES | values
    preferences["theme"] = "light"
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
    return preferences
