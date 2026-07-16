"""Stable workspace-calendar boundaries for library filters and durable scopes."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_FILTER_DATE = date(1, 1, 2)
MAX_FILTER_DATE = date(9999, 12, 30)


def normalize_calendar_date(value: object) -> str:
    """Return strict, safely representable YYYY-MM-DD text."""
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("date must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD") from exc
    if not MIN_FILTER_DATE <= parsed <= MAX_FILTER_DATE:
        raise ValueError("date is outside the supported range")
    return parsed.isoformat()


def normalize_timezone(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise ValueError("timezone must be a valid IANA timezone")
    value = value.strip()
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    return value


def calendar_date_ms(value: str, timezone_name: str, *, exclusive_end: bool = False) -> int:
    """Resolve a local calendar midnight to its exact UTC millisecond boundary."""
    local_day = date.fromisoformat(normalize_calendar_date(value))
    timezone = ZoneInfo(normalize_timezone(timezone_name))
    boundary = datetime.combine(local_day, datetime.min.time(), tzinfo=timezone)
    if exclusive_end:
        boundary += timedelta(days=1)
    return int(boundary.astimezone(UTC).timestamp() * 1000)


def resolve_date_scope(
    date_from: object,
    date_to: object,
    timezone_name: object,
    *,
    scope_version: int = 2,
) -> dict:
    """Build a complete, reproducible date boundary snapshot."""
    normalized_from = (
        normalize_calendar_date(date_from) if date_from not in (None, "") else None
    )
    normalized_to = normalize_calendar_date(date_to) if date_to not in (None, "") else None
    if normalized_from is None and normalized_to is None:
        return {}
    if normalized_from and normalized_to and normalized_from > normalized_to:
        raise ValueError("date_from must not follow date_to")
    timezone = normalize_timezone(timezone_name)
    scope = {"scope_version": scope_version, "date_timezone": timezone}
    if normalized_from is not None:
        scope["date_from"] = normalized_from
        scope["date_from_ms"] = calendar_date_ms(normalized_from, timezone)
    if normalized_to is not None:
        scope["date_to"] = normalized_to
        scope["date_to_ms_exclusive"] = calendar_date_ms(
            normalized_to, timezone, exclusive_end=True
        )
    return scope
