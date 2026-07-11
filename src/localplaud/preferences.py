"""Durable workspace display preferences."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from .db.models import KeyValue

PREFERENCES_KEY = "workspace_preferences"
DEFAULT_WORKSPACE_PREFERENCES = {
    "workspace_name": "localplaud",
    "theme": "system",
    "density": "comfortable",
    "timezone": "Asia/Taipei",
    "hour_cycle": "24",
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
    row = session.get(KeyValue, PREFERENCES_KEY)
    stored = row.value if row and isinstance(row.value, dict) else {}
    return DEFAULT_WORKSPACE_PREFERENCES | {
        key: stored[key] for key in DEFAULT_WORKSPACE_PREFERENCES if key in stored
    }


def save_workspace_preferences(session: Session, values: dict) -> dict:
    preferences = DEFAULT_WORKSPACE_PREFERENCES | values
    preferences["timezone"] = validate_timezone(str(preferences["timezone"]))
    row = session.get(KeyValue, PREFERENCES_KEY)
    if row is None:
        session.add(KeyValue(key=PREFERENCES_KEY, value=preferences))
    else:
        row.value = preferences
    session.flush()
    return preferences
