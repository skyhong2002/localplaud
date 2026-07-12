"""OpenCode Go text provider through the supported OpenCode CLI boundary."""

from __future__ import annotations

import json
import shutil
import subprocess

from ..config import OpenCodeGoLlmConfig
from .base import LLMError, LLMUnavailable


class OpenCodeGoLLM:
    name = "opencode-go"

    def __init__(self, cfg: OpenCodeGoLlmConfig):
        self.cfg = cfg

    @property
    def model(self) -> str:
        return self.cfg.model

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
            raise LLMError(
                f"OpenCode Go timed out after {self.cfg.timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise LLMUnavailable(f"could not start OpenCode CLI: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()[-1000:]
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
            raise LLMError("OpenCode Go returned no text completion")
        return completion
