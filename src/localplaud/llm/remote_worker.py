from __future__ import annotations

from ..providers.remote_inference import run_remote_json
from .base import LLMError, LLMUnavailable


class RemoteWorkerLLM:
    name = "remote-worker"

    def __init__(self, cfg):
        self.cfg = cfg
        self.usage = {}

    def available(self) -> bool:
        return bool(self.cfg.base_url and self.cfg.token and self.cfg.model)

    def complete(self, prompt: str, system: str | None = None, temperature: float = 0.3,
                 max_tokens: int = 2048) -> str:
        if not self.available():
            raise LLMUnavailable("remote-worker text provider is not configured")
        try:
            payload = run_remote_json(self.cfg, self.cfg.capability, {
                "prompt": prompt, "system": system, "temperature": temperature,
                "max_tokens": max_tokens,
            })
        except Exception as exc:
            raise LLMError(f"remote-worker text failed: {exc}") from exc
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise LLMError("remote-worker returned no text")
        self.usage = payload.get("usage", {})
        return text
