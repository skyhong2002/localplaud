"""Shared Jinja helpers for the Web App design system.

Registered once on the app's template environment (see ``register``):

- ``icon(name, size=None, label=None, cls="")`` renders an inline SVG that
  references the sprite in ``templates/_icons.html`` (included once by
  ``base.html``). Decorative by default (``aria-hidden``); pass ``label`` for a
  standalone meaningful icon.
- ``dur_long`` formats milliseconds the way the library rows do (``1h 22m 8s``).
- ``initials`` turns a workspace name into a two-letter avatar.
- ``recording_state`` maps a file summary dict to a compact processing-state
  descriptor (``None`` for a healthy, complete recording).
"""

from __future__ import annotations

import re

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
    env.filters["initials"] = initials
    env.globals["recording_state"] = recording_state
