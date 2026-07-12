"""Credential redaction for persisted and user-visible error diagnostics."""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<userinfo>[^/@\s]+@)",
    re.IGNORECASE,
)
_AUTHORIZATION_RE = re.compile(
    r"(?P<prefix>[\"']?authorization[\"']?\s*[:=]\s*)(?P<quote>[\"']?)"
    r"(?P<value>(?!\[REDACTED\])[^\"',;}\]]+)(?P=quote)",
    re.IGNORECASE,
)
_AUTH_SCHEME_RE = re.compile(
    r"\b(?P<scheme>Bearer|Basic)(?P<space>\s+)(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_OPENAI_KEY_RE = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_*.-]{6,}", re.IGNORECASE)
_ASSIGNMENT_RE = re.compile(
    r"""
    (?P<prefix>
        ["']?
        (?:
            api[ _-]?key
            | access[ _-]?token
            | refresh[ _-]?token
            | auth[ _-]?token
            | bearer[ _-]?token
            | client[ _-]?secret
            | session[ _-]?secret
            | worker[ _-]?token
            | hf[ _-]?token
            | token
            | secret
            | password
            | passwd
        )
        ["']?\s*[:=]\s*
    )
    (?P<quote>["']?)
    (?P<value>(?!\[REDACTED\])[^"'\s,;&}\]]+)
    (?P=quote)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def sanitize_error(value: object | None, *, max_length: int = 2000) -> str | None:
    """Return actionable error text with credential-shaped values removed."""
    if value is None:
        return None
    text = str(value)
    text = _URL_USERINFO_RE.sub(lambda match: f"{match.group('scheme')}{REDACTED}@", text)

    def redact_authorization(match: re.Match[str]) -> str:
        if match.group("value").lstrip().startswith("[REDACTED"):
            return match.group(0)
        return f"{match.group('prefix')}{match.group('quote')}{REDACTED}{match.group('quote')}"

    text = _AUTHORIZATION_RE.sub(redact_authorization, text)
    text = _AUTH_SCHEME_RE.sub(
        lambda match: f"{match.group('scheme')}{match.group('space')}{REDACTED}", text
    )
    text = _OPENAI_KEY_RE.sub(REDACTED, text)
    text = _ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{REDACTED}{match.group('quote')}"
        ),
        text,
    )
    if max_length < 0:
        raise ValueError("max_length must be non-negative")
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    return text[: max_length - 3] + "..."


def sanitize_error_value(value: Any, *, max_length: int = 2000) -> Any:
    """Recursively sanitize strings in structured diagnostic values."""
    if isinstance(value, str):
        return sanitize_error(value, max_length=max_length)
    if isinstance(value, dict):
        return {
            key: sanitize_error_value(item, max_length=max_length)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_error_value(item, max_length=max_length) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_error_value(item, max_length=max_length) for item in value)
    return value
