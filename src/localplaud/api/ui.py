"""Shared Jinja helpers for the Web App design system.

Registered once on the app's template environment (see ``register``):

- ``icon(name, size=None, label=None, cls="")`` renders an inline SVG that
  references the sprite in ``templates/_icons.html`` (included once by
  ``base.html``). Decorative by default (``aria-hidden``); pass ``label`` for a
  standalone meaningful icon.
- ``dur_long`` formats milliseconds the way the library rows do (``1h 22m 8s``);
  ``dur_short`` is the phone-row form (``1h 22m``).
- ``dt_rel`` is the shared relative list date (``Yesterday at 22:27`` / ``昨天 22:27``)
  in the workspace timezone, locale, and hour cycle.
- ``initials`` turns a workspace name into a two-letter avatar.
- ``recording_state`` maps a file summary dict to a compact processing-state
  descriptor (``None`` for a healthy, complete recording).
"""

from __future__ import annotations

import re

from jinja2 import pass_context
from markupsafe import Markup, escape

_ICON_NAME = re.compile(r"^[a-z0-9-]{1,40}$")
_SIZES = {"xs": "i-xs", "sm": "i-sm", "lg": "i-lg"}


def icon(name: str, size: str | None = None, label: str | None = None, cls: str = "") -> Markup:
    if not _ICON_NAME.match(name or ""):
        raise ValueError(f"invalid icon name: {name!r}")
    classes = " ".join(part for part in ("i", _SIZES.get(size or "", ""), cls) if part)
    if label:
        a11y = f'role="img" aria-label="{escape(label)}"'
    else:
        a11y = 'aria-hidden="true" focusable="false"'
    return Markup(f'<svg class="{escape(classes)}" {a11y}><use href="#i-{name}"></use></svg>')


def dur_long(ms: int | None) -> str:
    if not ms:
        return "—"
    seconds = int(ms // 1000)
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {sec}s"
    if minutes:
        return f"{minutes}m {sec}s"
    return f"{sec}s"


def dur_short(ms: int | None) -> str:
    """Phone-row duration like the Plaud app: ``1h 22m`` / ``8m`` / ``45s``."""
    if not ms:
        return "—"
    seconds = int(ms // 1000)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def relative_datetime(
    ms: int | None,
    *,
    timezone: str = "UTC",
    locale: str = "en",
    hour_cycle: str = "24",
    now=None,
) -> str:
    """Human list date: Today/Yesterday at HH:MM, ``30 Sep at 21:38``, or a full date.

    zh-TW: ``今天 22:27`` / ``昨天 22:27`` / ``9月30日 21:38`` / ``2025年9月30日``.
    """
    if not ms:
        return ""
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(timezone)
    except Exception:  # noqa: BLE001 - invalid stored timezone falls back to UTC
        zone = ZoneInfo("UTC")
    moment = datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(zone)
    today = (now or datetime.now(UTC)).astimezone(zone).date()
    if hour_cycle == "12":
        clock = moment.strftime("%I:%M %p").lstrip("0")
        if locale == "zh-Hant-TW":
            clock = ("上午 " if moment.hour < 12 else "下午 ") + moment.strftime("%I:%M").lstrip("0")
    else:
        clock = moment.strftime("%H:%M")
    zh = locale == "zh-Hant-TW"
    day = moment.date()
    if day == today:
        return f"今天 {clock}" if zh else f"Today at {clock}"
    if day == today - timedelta(days=1):
        return f"昨天 {clock}" if zh else f"Yesterday at {clock}"
    if day.year == today.year:
        return f"{day.month}月{day.day}日 {clock}" if zh else f"{day.day} {_MONTHS_EN[day.month - 1]} at {clock}"
    return f"{day.year}年{day.month}月{day.day}日" if zh else f"{day.day} {_MONTHS_EN[day.month - 1]} {day.year}"


def _dt_rel(context, ms: int | None) -> str:
    preferences = context.get("workspace_preferences") or {}
    return relative_datetime(
        ms,
        timezone=preferences.get("timezone", "UTC"),
        locale=preferences.get("locale", "en"),
        hour_cycle=str(preferences.get("hour_cycle", "24")),
    )


def initials(name: str | None) -> str:
    words = [word for word in re.split(r"[\s_\-]+", (name or "").strip()) if word]
    if not words:
        return "LP"
    if len(words) == 1:
        word = words[0]
        return (word[:2] if word.isascii() else word[:1]).upper()
    return (words[0][:1] + words[1][:1]).upper()


_PROCESSING = {"processing", "downloading"}
_QUEUED = {"downloaded", "discovered"}


def recording_state(item: dict) -> dict | None:
    """Return ``{"kind", "label"}`` for rows that are not healthy-complete.

    Labels are English source strings; templates pass them through ``t()``.
    """
    status = item.get("status")
    if status == "done" or item.get("is_trash"):
        return None
    if status in _PROCESSING:
        return {
            "kind": "processing",
            "label": "Downloading" if status == "downloading" else "Generating…",
        }
    if status in _QUEUED:
        return {"kind": "queued", "label": "Queued"}
    if status == "metadata_only":
        return {"kind": "cloud", "label": "In Plaud cloud"}
    if status == "partial":
        return {"kind": "degraded", "label": "Partly ready"}
    if status == "error":
        retry = item.get("retry") or {}
        if retry.get("next_at") and not retry.get("exhausted"):
            return {"kind": "attention", "label": "Failed · retry scheduled"}
        return {"kind": "attention", "label": "Needs attention"}
    return {"kind": "queued", "label": status or "Unknown"}


def asset_version() -> str:
    """Short content hash of the shared CSS/JS so browsers refetch after upgrades."""
    import hashlib
    from pathlib import Path

    static = Path(__file__).parent / "static"
    digest = hashlib.sha256()
    for folder in ("css", "js"):
        for path in sorted((static / folder).glob("*")):
            if path.is_file():
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def register(env) -> None:
    env.globals["asset_version"] = asset_version()
    env.globals["icon"] = icon
    env.filters["dur_long"] = dur_long
    env.filters["dur_short"] = dur_short
    env.filters["dt_rel"] = pass_context(_dt_rel)
    env.filters["initials"] = initials
    env.globals["recording_state"] = recording_state
