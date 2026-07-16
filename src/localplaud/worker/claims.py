"""Processing-claim identity shared by daemon and worker code."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

_DAEMON_TOKEN_PREFIX = "daemon"
_PROCESSING_OWNER: ContextVar[str | None] = ContextVar("processing_owner", default=None)


@dataclass(frozen=True)
class ProcessingClaim:
    file_id: str
    token: str


_PROCESSING_CLAIM: ContextVar[ProcessingClaim | None] = ContextVar(
    "processing_claim", default=None
)


@contextmanager
def processing_owner(owner: str | None) -> Iterator[None]:
    """Apply a daemon owner to claims created in the current execution context."""
    token = _PROCESSING_OWNER.set(owner)
    try:
        yield
    finally:
        _PROCESSING_OWNER.reset(token)


def current_processing_owner() -> str | None:
    return _PROCESSING_OWNER.get()


@contextmanager
def processing_claim(file_id: str, claim_token: str) -> Iterator[None]:
    """Fence writes in this execution context to one durable recording lease."""
    token = _PROCESSING_CLAIM.set(ProcessingClaim(file_id=file_id, token=claim_token))
    try:
        yield
    finally:
        _PROCESSING_CLAIM.reset(token)


def current_processing_claim() -> ProcessingClaim | None:
    return _PROCESSING_CLAIM.get()


def new_processing_token() -> str:
    owner = current_processing_owner()
    random_token = uuid.uuid4().hex
    if owner is None:
        return random_token
    return f"{_DAEMON_TOKEN_PREFIX}:{owner}:{random_token}"


def daemon_token_pattern(owner: str) -> str:
    return f"{_DAEMON_TOKEN_PREFIX}:{owner}:%"
