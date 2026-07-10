"""OpenAI (and OpenAI-compatible) LLM provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import LLMError, LLMUnavailable

if TYPE_CHECKING:
    from ..config import OpenAILlmConfig

log = logging.getLogger(__name__)


class OpenAILLM:
    """Chat completions via the OpenAI SDK. ``base_url`` lets this point at
    any OpenAI-compatible server."""

    name = "openai"

    def __init__(self, cfg: OpenAILlmConfig) -> None:
        self.cfg = cfg
        self.usage = {"input_tokens": 0, "output_tokens": 0, "provider_reported_tokens": True}

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
            raise LLMUnavailable("OpenAI LLM: no API key configured")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMUnavailable(
                "OpenAI LLM: the 'openai' package is not installed"
            ) from exc

        if self.cfg.max_input_chars is not None and len(prompt) > self.cfg.max_input_chars:
            raise LLMError("OpenAI LLM input exceeds configured max_input_chars")
        client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url or None,
                        default_headers=self.cfg.headers or None,
                        timeout=self.cfg.timeout_seconds)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = client.chat.completions.create(
            model=self.cfg.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if getattr(resp, "usage", None):
            self.usage["input_tokens"] += getattr(resp.usage, "prompt_tokens", 0) or 0
            self.usage["output_tokens"] += getattr(resp.usage, "completion_tokens", 0) or 0
        content = resp.choices[0].message.content
        if content is None:
            raise LLMError("OpenAI LLM: empty completion")
        return content
