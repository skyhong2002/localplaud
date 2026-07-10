"""Experimental single-user text provider through the supported Codex CLI boundary."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import LLMError, LLMUnavailable


class CodexLocalProvider:
    name = "codex-local"

    def __init__(self, cfg):
        self.cfg = cfg

    def available(self) -> bool:
        return bool(self.cfg.enabled and shutil.which(self.cfg.binary))

    def complete(self, prompt: str, system: str | None = None, temperature: float = 0.3,
                 max_tokens: int = 2048) -> str:
        if not self.cfg.enabled:
            raise LLMUnavailable("codex-local is experimental and explicitly disabled")
        binary = shutil.which(self.cfg.binary)
        if binary is None:
            raise LLMUnavailable(f"Codex CLI not found: {self.cfg.binary}")
        full_prompt = (
            "Return only the requested text. Do not inspect files, run commands, or mutate state.\n\n"
            + (f"System instructions:\n{system}\n\n" if system else "")
            + prompt
        )
        allowed_env = {key: value for key, value in os.environ.items() if key in {
            "HOME", "PATH", "CODEX_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "LANG", "LC_ALL",
        }}
        with tempfile.TemporaryDirectory(prefix="localplaud-codex-") as directory:
            output = Path(directory) / "answer.txt"
            command = [binary, "exec", "--ephemeral", "--ignore-user-config",
                       "--ignore-rules", "--sandbox", "read-only",
                       "--skip-git-repo-check", "--color", "never",
                       "--output-last-message", str(output), "-"]
            if self.cfg.model:
                command[2:2] = ["--model", self.cfg.model]
            try:
                result = subprocess.run(command, input=full_prompt, text=True,
                                        capture_output=True, timeout=self.cfg.timeout_seconds,
                                        cwd=directory, env=allowed_env, check=False)
            except subprocess.TimeoutExpired as exc:
                raise LLMError("codex-local completion timed out") from exc
            if result.returncode != 0:
                detail = (result.stderr or result.stdout)[-500:]
                raise LLMError(f"codex-local failed: {detail}")
            answer = output.read_text().strip() if output.exists() else result.stdout.strip()
            if not answer:
                raise LLMError("codex-local returned an empty completion")
            return answer
