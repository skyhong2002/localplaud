"""OpenAI (and OpenAI-compatible) LLM provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..config import get_settings
from ..openai_budget import (
    OpenAIBudgetBlocked,
    assert_openai_free_pool,
    is_real_openai_base_url,
)
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

    @property
    def model(self) -> str:
        return self.cfg.model

    @property
    def polish_chunk_chars(self) -> int:
        return self.cfg.polish_chunk_chars

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

        if is_real_openai_base_url(self.cfg.base_url):
            try:
                assert_openai_free_pool(
                    get_settings(),
                    model=self.cfg.model,
                    projected_tokens=(len(prompt) + len(system or "")) // 3 + max_tokens,
                )
            except OpenAIBudgetBlocked as exc:
                raise LLMError(str(exc)) from exc

        client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url or None)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        if self.cfg.api_mode == "responses":
            return self._complete_via_responses(
                client, messages, temperature, max_tokens, json_schema
            )

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

    def _complete_via_responses(
        self,
        client,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        json_schema: dict | None,
    ) -> str:
        """Stream the Responses API and return the assembled output text.

        Streaming is not an optimization here: a high reasoning effort spends
        minutes before the first output token, and a reverse proxy in front of
        a hosted relay terminates a request that produces no bytes for that
        long. The typed event stream carries reasoning progress throughout, so
        the connection never goes silent.
        """
        request: dict[str, object] = {
            "model": self.cfg.model,
            "input": messages,
            "max_output_tokens": max_tokens,
            "stream": True,
        }
        if self.cfg.reasoning_effort is not None:
            request["reasoning"] = {"effort": self.cfg.reasoning_effort}
        else:
            request["temperature"] = temperature
        if json_schema is not None:
            request["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "localplaud_response",
                    "strict": True,
                    "schema": json_schema,
                }
            }
        parts: list[str] = []
        incomplete: str | None = None
        completed = False
        for event in client.responses.create(**request):
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta":
                parts.append(event.delta)
            elif kind == "response.completed":
                completed = True
            elif kind == "response.failed":
                detail = getattr(getattr(event.response, "error", None), "message", None)
                raise LLMError(f"OpenAI LLM: response failed: {detail or 'unknown error'}")
            elif kind in {"error", "response.error"}:
                detail = getattr(getattr(event, "error", None), "message", None)
                detail = detail or getattr(event, "message", None)
                raise LLMError(f"OpenAI LLM: stream error: {detail or 'unknown error'}")
            elif kind == "response.incomplete":
                reason = getattr(
                    getattr(event.response, "incomplete_details", None), "reason", None
                )
                incomplete = reason or "unknown reason"
        if incomplete is not None:
            # Truncation here silently drops transcript, so it must not pass as
            # a usable completion.
            raise LLMError(f"OpenAI LLM: response incomplete ({incomplete})")
        if not completed:
            raise LLMError("OpenAI LLM: response stream ended before completion")
        content = "".join(parts)
        if not content.strip():
            raise LLMError("OpenAI LLM: empty completion")
        return content
