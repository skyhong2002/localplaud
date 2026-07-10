"""Ollama LLM provider — talks to a local Ollama server over HTTP."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import LLMError, LLMUnavailable

if TYPE_CHECKING:
    from ..config import OllamaConfig

log = logging.getLogger(__name__)


class OllamaProvider:
    """Chat completions via ``POST {host}/api/chat`` on a local Ollama server."""

    name = "ollama"

    def __init__(self, cfg: OllamaConfig) -> None:
        self.cfg = cfg

    def available(self) -> bool:
        return self.health()[0]

    def health(self) -> tuple[bool, str]:
        from ..ollama import model_health

        return model_health(self.cfg.host, self.cfg.model)

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> str:
        import httpx

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            resp = httpx.post(
                f"{self.cfg.host}/api/chat",
                json={
                    "model": self.cfg.model,
                    "messages": messages,
                    "stream": False,
                    # Keep the token budget for visible output. Thinking-capable
                    # local models can otherwise spend the whole response on a
                    # hidden reasoning field and return empty content.
                    "think": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                    },
                },
                timeout=600,
            )
        except httpx.ConnectError as exc:
            raise LLMUnavailable(
                f"cannot reach Ollama at {self.cfg.host}: {exc}"
            ) from exc
        if resp.status_code != 200:
            if resp.status_code == 404:
                from ..ollama import response_error

                error = response_error(resp)
                if "model" in error.lower() and "not found" in error.lower():
                    raise LLMUnavailable(
                        f"Ollama model {self.cfg.model!r} is not installed; "
                        f"run `ollama pull {self.cfg.model}`"
                    )
            raise LLMError(
                f"Ollama returned HTTP {resp.status_code}: {resp.text[:500]}"
            )
        content = resp.json().get("message", {}).get("content", "")
        if not content.strip():
            raise LLMError("Ollama LLM returned an empty completion")
        return content
