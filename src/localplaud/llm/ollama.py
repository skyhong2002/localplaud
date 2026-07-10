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
        """True if the Ollama server answers on ``/api/tags``."""
        try:
            import httpx

            resp = httpx.get(f"{self.cfg.host}/api/tags", timeout=5)
            return resp.status_code == 200
        except Exception:  # noqa: BLE001 - any failure means unavailable
            return False

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
                    "options": {"temperature": temperature},
                },
                timeout=600,
            )
        except httpx.ConnectError as exc:
            raise LLMUnavailable(
                f"cannot reach Ollama at {self.cfg.host}: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise LLMError(
                f"Ollama returned HTTP {resp.status_code}: {resp.text[:500]}"
            )
        return resp.json()["message"]["content"]
