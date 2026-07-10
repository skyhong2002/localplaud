"""Anthropic (Claude) LLM provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import LLMUnavailable

if TYPE_CHECKING:
    from ..config import AnthropicLlmConfig

log = logging.getLogger(__name__)


class AnthropicLLM:
    """Completions via the Anthropic Messages API."""

    name = "anthropic"

    def __init__(self, cfg: AnthropicLlmConfig) -> None:
        self.cfg = cfg

    def available(self) -> bool:
        """True if an API key is configured."""
        return bool(self.cfg.api_key)

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> str:
        if not self.cfg.api_key:
            raise LLMUnavailable("Anthropic LLM: no API key configured")
        try:
            import anthropic
        except ImportError as exc:
            raise LLMUnavailable(
                "Anthropic LLM: the 'anthropic' package is not installed"
            ) from exc

        client = anthropic.Anthropic(api_key=self.cfg.api_key)
        resp = client.messages.create(
            model=self.cfg.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system if system is not None else anthropic.NOT_GIVEN,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text
            for block in resp.content
            if getattr(block, "type", None) == "text"
        )
