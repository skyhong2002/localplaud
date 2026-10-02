"""Ollama LLM provider — talks to a local Ollama server over HTTP."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import LLMError, LLMInputTooLarge, LLMOutputInvalid, LLMTransientError, LLMUnavailable

if TYPE_CHECKING:
    from ..config import OllamaConfig

log = logging.getLogger(__name__)


class OllamaProvider:
    """Chat completions via ``POST {host}/api/chat`` on a local Ollama server."""

    name = "ollama"

    def __init__(self, cfg: OllamaConfig) -> None:
        self.cfg = cfg

    @property
    def model(self) -> str:
        return self.cfg.model

    @property
    def polish_chunk_chars(self) -> int:
        return self.cfg.polish_chunk_chars

    @property
    def summary_max_chunk_chars(self) -> int:
        return self.cfg.summary_chunk_chars

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
        json_schema: dict | None = None,
    ) -> str:
        import httpx

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": False,
            "truncate": False,
            # Keep the token budget for visible output. Thinking-capable
            # local models can otherwise spend the whole response on a
            # hidden reasoning field and return empty content.
            "think": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": self.cfg.context_tokens,
            },
        }
        if json_schema is not None:
            payload["format"] = json_schema
        try:
            resp = httpx.post(
                f"{self.cfg.host}/api/chat",
                json=payload,
                timeout=self.cfg.timeout_seconds,
            )
        except httpx.ConnectError as exc:
            raise LLMUnavailable(
                f"cannot reach Ollama at {self.cfg.host}: {exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise LLMTransientError(
                f"Ollama did not answer within {self.cfg.timeout_seconds}s "
                f"(model {self.cfg.model!r})"
            ) from exc
        if resp.status_code != 200:
            if resp.status_code == 400 and "context" in resp.text.lower():
                raise LLMInputTooLarge(
                    "Ollama input exceeds its context window; reduce the stage chunk size "
                    "or increase llm.ollama.context_tokens"
                )
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
        result = resp.json()
        if result.get("done_reason") == "length":
            raise LLMOutputInvalid(
                "Ollama output reached its token limit; the incomplete result was rejected"
            )
        content = result.get("message", {}).get("content", "")
        if not content.strip():
            raise LLMError("Ollama LLM returned an empty completion")
        return content

    def stream(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ):
        """Yield completion text incrementally (``/api/chat`` with ``stream: true``).

        Used by interactive Ask; batch stages keep using :meth:`complete`.
        """
        import json

        import httpx

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "stream": True,
            "truncate": False,
            "think": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": self.cfg.context_tokens,
            },
        }
        produced = False
        try:
            with httpx.stream(
                "POST",
                f"{self.cfg.host}/api/chat",
                json=payload,
                timeout=self.cfg.timeout_seconds,
            ) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode("utf-8", "replace")
                    if resp.status_code == 400 and "context" in body.lower():
                        raise LLMInputTooLarge(
                            "Ollama input exceeds its context window; reduce the stage chunk "
                            "size or increase llm.ollama.context_tokens"
                        )
                    if resp.status_code == 404 and "not found" in body.lower():
                        raise LLMUnavailable(
                            f"Ollama model {self.cfg.model!r} is not installed; "
                            f"run `ollama pull {self.cfg.model}`"
                        )
                    raise LLMError(f"Ollama returned HTTP {resp.status_code}: {body[:500]}")
                for line in resp.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError as exc:
                        raise LLMError("Ollama returned a malformed stream event") from exc
                    if event.get("error"):
                        raise LLMError(f"Ollama stream error: {str(event['error'])[:300]}")
                    piece = (event.get("message") or {}).get("content", "")
                    if piece:
                        produced = True
                        yield piece
                    if event.get("done"):
                        if event.get("done_reason") == "length":
                            raise LLMOutputInvalid(
                                "Ollama output reached its token limit; the incomplete result "
                                "was rejected"
                            )
                        break
        except httpx.ConnectError as exc:
            raise LLMUnavailable(f"cannot reach Ollama at {self.cfg.host}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise LLMTransientError(
                f"Ollama did not answer within {self.cfg.timeout_seconds}s "
                f"(model {self.cfg.model!r})"
            ) from exc
        if not produced:
            raise LLMError("Ollama LLM returned an empty completion")
