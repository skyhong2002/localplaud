"""Read the ChatGPT subscription window of a local Codex login.

The AI gateway spends the same subscription, so its reserve is checked here
before every call. This module never runs a model turn.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import time
from pathlib import Path

from ..config import CodexQuotaConfig
from .base import LLMQuotaExhausted, LLMUnavailable


class _QuotaCheckTimeout(LLMUnavailable):
    """The local app-server did not report the subscription window in time."""


class CodexQuotaReader:
    """Query ``codex app-server`` for the remaining subscription window."""

    def __init__(self, cfg: CodexQuotaConfig, *, expected_account_id: str | None = None):
        self.cfg = cfg
        # When another client spends this subscription, its reserve is only
        # meaningful if the local login reports that same account.
        self.expected_account_id = expected_account_id

    def _executable(self) -> str | None:
        return shutil.which(self.cfg.executable)

    def _environment(self) -> dict[str, str]:
        env = {
            "HOME": str(Path.home()),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        }
        for key in ("LANG", "LC_ALL", "SSL_CERT_FILE", "CODEX_CA_CERTIFICATE"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        if self.cfg.codex_home:
            env["CODEX_HOME"] = str(Path(self.cfg.codex_home).expanduser())
        return env

    @staticmethod
    def _remaining_from_rate_limit_result(result: dict) -> int:
        buckets = result.get("rateLimitsByLimitId") or {}
        snapshot = buckets.get("codex") or result.get("rateLimits") or {}
        primary = snapshot.get("primary") or {}
        used = primary.get("usedPercent")
        if not isinstance(used, int) or isinstance(used, bool) or not 0 <= used <= 100:
            raise LLMUnavailable("Codex subscription quota response was incomplete")
        # A reported secondary window can be the tighter limit; honour whichever
        # leaves less. Plans without one report null.
        secondary = (snapshot.get("secondary") or {}).get("usedPercent")
        if isinstance(secondary, int) and not isinstance(secondary, bool) and 0 <= secondary <= 100:
            used = max(used, secondary)
        return 100 - used

    def _check_quota_account(self, result: dict) -> None:
        if self.expected_account_id is not None and result.get("accountId") != (
            self.expected_account_id
        ):
            raise LLMUnavailable(
                "Codex quota login does not report the configured subscription account"
            )

    def _remaining_quota_percent(self) -> int:
        """Read the Codex subscription window without spending a model turn."""
        executable = self._executable()
        if executable is None:
            raise LLMUnavailable(f"{self.cfg.executable} is not on PATH")
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                [executable, "app-server", "--stdio"],
                text=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=1,
                env=self._environment(),
            )
            if process.stdin is None or process.stdout is None:
                raise LLMUnavailable("Codex quota check could not open app-server pipes")
            messages = (
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {"name": "localplaud", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                },
                {"jsonrpc": "2.0", "method": "initialized", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "account/rateLimits/read",
                    "params": {},
                },
            )
            for message in messages:
                process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + self.cfg.quota_check_timeout_seconds
            while time.monotonic() < deadline:
                readable, _, _ = select.select(
                    [process.stdout], [], [], min(0.5, max(0.0, deadline - time.monotonic()))
                )
                if not readable:
                    if process.poll() is not None:
                        break
                    continue
                line = process.stdout.readline()
                if not line:
                    break
                response = json.loads(line)
                if response.get("id") != 2:
                    continue
                if response.get("error"):
                    raise LLMUnavailable("Codex subscription quota could not be read")
                result = response.get("result") or {}
                self._check_quota_account(result)
                return self._remaining_from_rate_limit_result(result)
            raise _QuotaCheckTimeout("Codex subscription quota check timed out")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise LLMUnavailable("Codex subscription quota check failed") from exc
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()

    def _ensure_quota_reserve(self) -> int:
        try:
            remaining = self._remaining_quota_percent()
        except _QuotaCheckTimeout:
            # A slow app-server start occasionally misses the window. One fresh
            # read is cheap; a second timeout still fails closed.
            remaining = self._remaining_quota_percent()
        minimum = self.cfg.quota_reserve_percent + self.cfg.quota_call_headroom_percent
        if remaining <= minimum:
            raise LLMQuotaExhausted(
                "Codex subscription reserve protected: "
                f"{remaining}% remains; a new call requires more than {minimum}%"
            )
        return remaining
