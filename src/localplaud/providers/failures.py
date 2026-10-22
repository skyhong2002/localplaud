"""Explicit provider failure classes used at fallback boundaries."""

from __future__ import annotations

import httpx


class ProviderFailure(RuntimeError):
    """Base class for failures intentionally classified by a provider adapter."""


class ProviderUnavailableError(ProviderFailure):
    """The selected provider cannot currently serve the requested capability."""


class RetryableProviderError(ProviderFailure):
    """A transient provider failure for which an explicit fallback is allowed."""


def allows_provider_fallback(exc: BaseException) -> bool:
    """Return true only for deliberately classified failures, including wrappers."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, (ProviderUnavailableError, RetryableProviderError)):
            return True
        seen.add(id(current))
        # Only an explicit ``raise ... from exc`` is a provider adapter's
        # deliberate classification.  An implicit context may instead be a
        # programming bug raised while handling a retryable provider error.
        current = current.__cause__
    return False


def is_retryable_sdk_failure(exc: BaseException) -> bool:
    """Classify transport/timeouts, rate limits, and server failures only."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, ConnectionError, httpx.TransportError)):
            return True
        status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        if isinstance(status, int) and (status in {408, 425, 429} or status >= 500):
            return True
        current = current.__cause__
    return False


def sdk_failure_label(exc: BaseException) -> str:
    """Return a non-secret diagnostic label without provider response bodies."""
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    suffix = f" HTTP {status}" if isinstance(status, int) else ""
    return f"{type(exc).__name__}{suffix}"
