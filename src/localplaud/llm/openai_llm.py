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

    def available(self) -> bool:
        """True if an API key is configured."""
        return bool(self.cfg.api_key)

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        json_schema: dict | None = None,
    ) -> str:
        if not self.cfg.api_key:
            raise LLMUnavailable("OpenAI LLM: no API key configured")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMUnavailable(
                "OpenAI LLM: the 'openai' package is not installed"
            ) from exc

        client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url or None)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        request: dict[str, object] = {
            "model": self.cfg.model,
            "messages": messages,
        }
        if self.cfg.reasoning_effort is not None:
            request["reasoning_effort"] = self.cfg.reasoning_effort
            request["max_completion_tokens"] = max_tokens
        else:
            request["temperature"] = temperature
            request["max_tokens"] = max_tokens
        if json_schema is not None:
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "localplaud_response",
                    "strict": True,
                    "schema": json_schema,
                },
            }
        resp = client.chat.completions.create(
            **request,
        )
        content = resp.choices[0].message.content
        if content is None:
            raise LLMError("OpenAI LLM: empty completion")
        return content
