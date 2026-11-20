"""Read-only month calendar of recordings in the workspace timezone.

Each day counts non-trash recordings whose ``start_time_ms`` falls inside that
local calendar day. Day boundaries come from :func:`calendar_date_ms`, the same
helper the library ``date_from``/``date_to`` filters use, so a calendar count
always equals the number of rows the linked Files view shows (with no other
filters applied).
"""

from __future__ import annotations

import calendar as _calendar
import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .date_filters import calendar_date_ms, normalize_timezone
from .db.models import PlaudFile

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_MIN_MONTH = (1, 2)  # 0001-01 cannot be resolved at every UTC offset.
_MAX_MONTH = (9999, 11)  # The exclusive end of 9999-12 overflows ``date``.

WEEKDAYS = (
    ("Sun", "Sunday"),
    ("Mon", "Monday"),
    ("Tue", "Tuesday"),
    ("Wed", "Wednesday"),
    ("Thu", "Thursday"),
    ("Fri", "Friday"),
    ("Sat", "Saturday"),
)
_EN_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_ZH_WEEKDAYS = ("日", "一", "二", "三", "四", "五", "六")


def workspace_today(timezone_name: str) -> date:
    return datetime.now(ZoneInfo(normalize_timezone(timezone_name))).date()


def parse_month(value: str | None) -> tuple[int, int] | None:
    match = _MONTH_RE.match(value or "")
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12 or not _MIN_MONTH <= (year, month) <= _MAX_MONTH:
        return None
    return year, month


def _shift(year: int, month: int, delta: int) -> tuple[int, int] | None:
    index = year * 12 + (month - 1) + delta
    shifted = (index // 12, index % 12 + 1)
    return shifted if _MIN_MONTH <= shifted <= _MAX_MONTH else None


def _month_key(value: tuple[int, int] | None) -> str | None:
    return f"{value[0]:04d}-{value[1]:02d}" if value else None


def month_title(year: int, month: int, locale: str) -> str:
    if locale == "zh-Hant-TW":
        return f"{year} 年 {month} 月"
    return f"{_EN_MONTHS[month - 1]} {year}"


def day_label(day: date, count: int, locale: str) -> str:
    # Python weekday(): Monday=0; our tables are Sunday-first.
    weekday = (day.weekday() + 1) % 7
    if locale == "zh-Hant-TW":
        recordings = f"{count} 份錄音" if count else "沒有錄音"
        return f"{day.year} 年 {day.month} 月 {day.day} 日 星期{_ZH_WEEKDAYS[weekday]}，{recordings}"
    noun = "recording" if count == 1 else "recordings"
    recordings = f"{count} {noun}" if count else "no recordings"
    return (
        f"{WEEKDAYS[weekday][1]}, {_EN_MONTHS[day.month - 1]} {day.day}, {day.year}, {recordings}"
    )


def day_counts(session, year: int, month: int, timezone_name: str) -> dict[date, int]:
    """Count non-trash recordings per workspace-local day of one month."""
    tz_name = normalize_timezone(timezone_name)
    tz = ZoneInfo(tz_name)
    first = date(year, month, 1)
    last = date(year, month, _calendar.monthrange(year, month)[1])
    start_ms = calendar_date_ms(first.isoformat(), tz_name)
    end_ms = calendar_date_ms(last.isoformat(), tz_name, exclusive_end=True)
    counts: dict[date, int] = {}
    for start_time_ms in session.scalars(
        select(PlaudFile.start_time_ms).where(
            PlaudFile.is_trash.is_(False),
            PlaudFile.start_time_ms.is_not(None),
            PlaudFile.start_time_ms >= start_ms,
            PlaudFile.start_time_ms < end_ms,
        )
    ):
        local_day = datetime.fromtimestamp(start_time_ms / 1000, tz=UTC).astimezone(tz).date()
        counts[local_day] = counts.get(local_day, 0) + 1
    return counts


def build_month(
    session,
    month_param: str | None,
    timezone_name: str,
    locale: str,
    *,
    today: date | None = None,
) -> dict:
    """Template context for one Sunday-first month grid."""
    today = today or workspace_today(timezone_name)
    current = (today.year, today.month)
    requested = parse_month(month_param)
    year, month = requested or current
    counts = day_counts(session, year, month, timezone_name)
    weeks = []
    for week in _calendar.Calendar(firstweekday=6).monthdatescalendar(year, month):
        cells = []
        for day in week:
            in_month = day.month == month
            count = counts.get(day, 0) if in_month else 0
            iso = day.isoformat()
            cells.append(
                {
                    "date": iso,
                    "day": day.day,
                    "in_month": in_month,
                    "count": count,
                    "is_today": day == today,
                    "label": day_label(day, count, locale),
                    "href": f"/?date_from={iso}&date_to={iso}" if in_month and count else None,
                }
            )
        weeks.append(cells)
    return {
        "year": year,
        "month": month,
        "key": _month_key((year, month)),
        "title": month_title(year, month, locale),
        "weeks": weeks,
        "weekdays": WEEKDAYS,
        "total": sum(counts.values()),
        "active_days": len(counts),
        "prev": _month_key(_shift(year, month, -1)),
        "next": _month_key(_shift(year, month, 1)),
        "current": _month_key(current),
        "is_current": (year, month) == current,
        "invalid_request": bool(month_param) and requested is None,
        "month_from": date(year, month, 1).isoformat(),
        "month_to": date(year, month, _calendar.monthrange(year, month)[1]).isoformat(),
        "timezone": normalize_timezone(timezone_name),
    }
