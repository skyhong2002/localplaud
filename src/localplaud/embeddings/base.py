"""Embedding provider interface (semantic search / Q&A indexing).

Same pattern as the ASR and LLM providers: a Protocol contract plus a factory
dispatching on ``EmbeddingsConfig.provider``, with all optional dependencies
imported lazily inside the provider modules.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..config import EmbeddingsConfig

log = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    """Raised when a provider fails to embed."""


class EmbeddingUnavailable(EmbeddingError):
    """Raised when a provider can't run in this environment (missing
    dependency, model, or API key)."""


@runtime_checkable
class Embedder(Protocol):
    """Contract for all embedding providers."""

    name: str

    @property
    def dim(self) -> int:
        """Dimensionality of the vectors this embedder produces."""
        ...

    def available(self) -> bool:
        """Cheap check: can this provider run here right now?"""
        ...

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts``, one vector per input, in order. Raise
        :class:`EmbeddingUnavailable` if the provider can't run,
        :class:`EmbeddingError` for a hard failure."""
        ...


def build_embedder(cfg: EmbeddingsConfig) -> Embedder:
    """Construct the embedder selected by ``cfg.provider``.

    Provider modules are imported lazily so unused providers (and their
    optional SDKs) cost nothing at import time.
    """
    if cfg.provider == "local":
        from .local import LocalEmbedder

        return LocalEmbedder(cfg.local)
    if cfg.provider == "openai":
        from .openai_embed import OpenAIEmbedder

        return OpenAIEmbedder(cfg.openai)
    raise EmbeddingUnavailable(f"unknown embedding provider: {cfg.provider!r}")
