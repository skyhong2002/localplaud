"""OpenCode Go text provider through the supported OpenCode CLI boundary."""

from __future__ import annotations

import json
import shutil
import subprocess

from ..config import OpenCodeGoLlmConfig
from .base import (
    LLMError,
    LLMInputTooLarge,
    LLMQuotaExhausted,
    LLMTransientError,
    LLMUnavailable,
)

_QUOTA_MARKERS = (
    "429",
    "quota",
    "rate limit",
    "usage limit",
    "usage exhausted",
    "credits exhausted",
)
_TRANSIENT_MARKERS = (
    "500",
    "502",
    "503",
    "504",
    "connection",
    "disconnected",
    "eof",
    "internal server error",
    "network",
    "peer closed",
    "stream reset",
    "timeout",
    "timed out",
)
_CONTEXT_MARKERS = (
    "context window",
    "input too large",
    "maximum context length",
    "prompt too long",
    "too many tokens",
)


class OpenCodeGoLLM:
    name = "opencode-go"

    def __init__(self, cfg: OpenCodeGoLlmConfig):
        self.cfg = cfg

    @property
    def model(self) -> str:
        return self.cfg.model

    @property
    def polish_chunk_chars(self) -> int:
        return self.cfg.polish_chunk_chars

    def available(self) -> bool:
        return shutil.which(self.cfg.executable) is not None

    def health(self) -> tuple[bool, str]:
        executable = shutil.which(self.cfg.executable)
        if executable is None:
            return False, f"{self.cfg.executable} is not on PATH"
        try:
            credentials = subprocess.run(
                [self.cfg.executable, "providers", "list"],
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            models = subprocess.run(
                [self.cfg.executable, "models", "opencode-go"],
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"OpenCode health check failed: {exc}"
        if credentials.returncode != 0 or "OpenCode Go" not in credentials.stdout:
            return False, "OpenCode Go credential is not configured"
        expected = f"opencode-go/{self.cfg.model}"
        if models.returncode != 0 or expected not in models.stdout.splitlines():
            return False, f"model {expected} is not available"
        return True, f"OpenCode Go credential and model {expected} verified"

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        json_schema: dict | None = None,
    ) -> str:
        # The selected OpenCode agent owns limits and output validation remains
        # in the calling stage.
        del temperature, max_tokens, json_schema
        if not self.available():
            raise LLMUnavailable(f"{self.cfg.executable} is not on PATH")
        payload = f"{system}\n\n{prompt}" if system else prompt
        command = [
            self.cfg.executable,
            "run",
            "--model",
            f"opencode-go/{self.cfg.model}",
            "--agent",
            self.cfg.agent,
            "--format",
            "json",
            "--title",
            "localplaud transcript polish",
        ]
        try:
            result = subprocess.run(
                command,
                input=payload,
                text=True,
                capture_output=True,
                timeout=self.cfg.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMTransientError(
                f"OpenCode Go timed out after {self.cfg.timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise LLMUnavailable(f"could not start OpenCode CLI: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()[-1000:]
            normalized = detail.lower()
            if any(marker in normalized for marker in _CONTEXT_MARKERS):
                raise LLMInputTooLarge("OpenCode Go input exceeded the model context")
            if any(marker in normalized for marker in _QUOTA_MARKERS):
                raise LLMQuotaExhausted("OpenCode Go usage is exhausted")
            if any(marker in normalized for marker in _TRANSIENT_MARKERS):
                raise LLMTransientError("OpenCode Go transport failed")
            raise LLMError(f"OpenCode Go failed: {detail}")

        parts: list[str] = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "text":
                text = (event.get("part") or {}).get("text")
                if text:
                    parts.append(str(text))
        completion = "".join(parts).strip()
        if not completion:
            raise LLMTransientError("OpenCode Go returned no text completion")
        return completion
