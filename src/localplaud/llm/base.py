"""LLM provider interface (summaries, templated notes, Q&A).

Mirrors the ASR provider pattern: a small Protocol every provider satisfies,
plus a factory that dispatches on ``LlmConfig.provider``. Provider modules
import their SDKs lazily so importing localplaud never requires optional
dependencies to be installed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..config import LlmConfig

log = logging.getLogger(__name__)

_RESOLVED_MODELS: ContextVar[dict[str, list[str]] | None] = ContextVar(
    "llm_resolved_models", default=None
)


def note_resolved_model(requested: str, resolved: str | None) -> None:
    """Record which model answered a request for ``requested`` (an alias or name)."""
    models = _RESOLVED_MODELS.get()
    if models is None or not resolved:
        return
    seen = models.setdefault(requested, [])
    if resolved not in seen:
        seen.append(resolved)


@contextmanager
def capture_resolved_models() -> Iterator[dict[str, list[str]]]:
    """Collect requested -> answering models for the calls made in this block."""
    models: dict[str, list[str]] = {}
    token = _RESOLVED_MODELS.set(models)
    try:
        yield models
    finally:
        _RESOLVED_MODELS.reset(token)


class LLMError(RuntimeError):
    """Raised when a provider fails to produce a completion."""


class LLMUnavailable(LLMError):
    """Raised when a provider can't run in this environment (missing
    dependency, API key, or unreachable server)."""


class LLMTransientError(LLMError):
    """Raised for transport, timeout, or temporary provider failures."""


class LLMTimeout(LLMTransientError):
    """Raised when one request exceeds the provider's per-call time limit.

    Unlike transport or quota failures, a timeout is often caused by request
    size, so chunked stages may retry the same content as smaller requests.
    """


class LLMQuotaExhausted(LLMTransientError):
    """Raised when a provider reports an explicit quota or usage limit."""


class LLMOutputInvalid(LLMError):
    """Raised when a provider response cannot satisfy the stage contract."""


class LLMContentFiltered(LLMOutputInvalid):
    """The provider's safety filter stopped a response for this specific input.

    Retrying the same request cannot help, but a smaller request that leaves out the
    refused passage can. Stages therefore split and isolate instead of failing whole.
    """


class LLMInputTooLarge(LLMOutputInvalid):
    """Raised when a provider rejects a request that must be split smaller."""


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
        json_schema: dict | None = None,
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
    if cfg.provider == "opencode-go":
        from .opencode_go import OpenCodeGoLLM

        return OpenCodeGoLLM(cfg.opencode_go)
    if cfg.provider == "codex-local":
        # Historical profiles may still name the retired Codex CLI text backend.
        raise LLMUnavailable(
            "the codex-local text provider was removed; select the ai-gateway "
            "connection (sky-quality alias) for this stage"
        )
    if cfg.provider == "ai-gateway":
        from .ai_gateway import AiGatewayLLM

        return AiGatewayLLM(cfg.ai_gateway)
    raise LLMUnavailable(f"unknown LLM provider: {cfg.provider!r}")
