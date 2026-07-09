"""LLM provider interface (summaries, templated notes, Q&A).

Mirrors the ASR provider pattern: a small Protocol every provider satisfies,
plus a factory that dispatches on ``LlmConfig.provider``. Provider modules
import their SDKs lazily so importing localplaud never requires optional
dependencies to be installed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..config import LlmConfig

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Raised when a provider fails to produce a completion."""


class LLMUnavailable(LLMError):
    """Raised when a provider can't run in this environment (missing
    dependency, API key, or unreachable server)."""


@runtime_checkable
class LLMProvider(Protocol):
    """Contract for all LLM providers."""

    name: str

    def available(self) -> bool:
        """Cheap check: can this provider run here right now (dep installed,
        API key set, server reachable)?"""
        ...

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> str:
        """Return the completion text for ``prompt``. Raise
        :class:`LLMUnavailable` if the provider can't run, :class:`LLMError`
        for a hard failure."""
        ...


def build_llm(cfg: LlmConfig) -> LLMProvider:
    """Construct the provider selected by ``cfg.provider``.

    Provider modules are imported lazily so unused providers (and their
    optional SDKs) cost nothing at import time.
    """
    if cfg.provider == "ollama":
        from .ollama import OllamaProvider

        return OllamaProvider(cfg.ollama)
    if cfg.provider == "openai":
        from .openai_llm import OpenAILLM

        return OpenAILLM(cfg.openai)
    if cfg.provider == "anthropic":
        from .anthropic_llm import AnthropicLLM

        return AnthropicLLM(cfg.anthropic)
    raise LLMUnavailable(f"unknown LLM provider: {cfg.provider!r}")
