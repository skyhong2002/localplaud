"""Text provider for a central OpenAI-compatible gateway with model aliases."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from ..config import CodexQuotaConfig
from .base import (
    LLMError,
    LLMInputTooLarge,
    LLMQuotaExhausted,
    LLMTimeout,
    LLMTransientError,
    LLMUnavailable,
)
from .codex_quota import CodexQuotaReader
from .openai_llm import OpenAILLM

if TYPE_CHECKING:
    from ..config import AiGatewayLlmConfig

log = logging.getLogger(__name__)

_CONTEXT_MARKERS = (
    "context window",
    "input too large",
    "maximum context length",
    "prompt too long",
    "too many tokens",
)


class AiGatewayLLM(OpenAILLM):
    """Request a semantic alias; the gateway decides which model answers."""

    name = "ai-gateway"

    def __init__(self, cfg: AiGatewayLlmConfig) -> None:
        super().__init__(cfg)  # type: ignore[arg-type]
        self._reserve_lock = threading.Lock()
        self._reserve_checked_at: float | None = None

    @property
    def summary_chunk_chars(self) -> int:
        return self.cfg.summary_chunk_chars

    def available(self) -> bool:
        return bool(self.cfg.api_key and self.cfg.base_url)

    def _client(self, openai_class):
        return openai_class(
            api_key=self.cfg.api_key,
            base_url=self.cfg.base_url,
            timeout=float(self.cfg.timeout_seconds),
            max_retries=0,
        )

    def _call_time_limit(self) -> int | None:
        return self.cfg.timeout_seconds

    def _quota_reader(self) -> CodexQuotaReader:
        return CodexQuotaReader(
            CodexQuotaConfig(
                executable=self.cfg.quota_executable,
                codex_home=self.cfg.quota_codex_home,
                quota_reserve_percent=self.cfg.quota_reserve_percent,
                quota_call_headroom_percent=self.cfg.quota_call_headroom_percent,
                quota_check_timeout_seconds=self.cfg.quota_check_timeout_seconds,
            ),
            expected_account_id=self.cfg.quota_account_id,
        )

    _RESERVE_REUSE_SECONDS = 10.0

    def _ensure_reserve_cached(self) -> None:
        if not self.cfg.quota_guard:
            return
        with self._reserve_lock:
            checked = self._reserve_checked_at
            if checked is not None and time.monotonic() - checked < self._RESERVE_REUSE_SECONDS:
                return
            self._reserve_checked_at = None
            self._quota_reader()._ensure_quota_reserve()
            self._reserve_checked_at = time.monotonic()

    def health(self) -> tuple[bool, str]:
        if not self.available():
            return False, "gateway base URL or client key is not configured"
        if not self.cfg.quota_guard:
            return True, f"gateway alias {self.cfg.model}; subscription reserve guard is off"
        try:
            remaining = self._quota_reader()._ensure_quota_reserve()
        except (LLMUnavailable, LLMQuotaExhausted) as exc:
            return False, str(exc)
        return True, (
            f"gateway alias {self.cfg.model}; {remaining}% of the subscription window "
            f"remains; {self.cfg.quota_reserve_percent}% is protected"
        )

    def complete(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
        json_schema: dict | None = None,
    ) -> str:
        if not self.available():
            raise LLMUnavailable("AI gateway: base URL or client key is not configured")
        # Fail closed before spending the shared subscription. A reading this
        # fresh is reused so overlapping calls do not each start a Codex process;
        # a failed or exhausted reading is never cached.
        self._ensure_reserve_cached()
        try:
            import openai
        except ImportError as exc:
            raise LLMUnavailable("AI gateway: the 'openai' package is not installed") from exc
        try:
            return super().complete(
                prompt,
                system=system,
                temperature=temperature,
                max_tokens=max_tokens,
                json_schema=json_schema,
            )
        except (openai.APITimeoutError, httpx.TimeoutException) as exc:
            # A stream that stalls mid-response surfaces as a raw httpx error.
            raise LLMTimeout(f"AI gateway timed out after {self.cfg.timeout_seconds}s") from exc
        except (openai.APIConnectionError, httpx.TransportError) as exc:
            raise LLMTransientError("AI gateway connection failed") from exc
        except openai.RateLimitError as exc:
            raise LLMQuotaExhausted("AI gateway reported a usage limit") from exc
        except openai.AuthenticationError as exc:
            raise LLMUnavailable("AI gateway rejected the client key") from exc
        except openai.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LLMTransientError(f"AI gateway failed with HTTP {exc.status_code}") from exc
            if any(marker in str(exc).lower() for marker in _CONTEXT_MARKERS):
                raise LLMInputTooLarge("AI gateway input exceeded the model context") from exc
            raise LLMError(f"AI gateway rejected the request with HTTP {exc.status_code}") from exc
        except openai.APIError as exc:
            # An error event inside the stream carries no HTTP status.
            if any(marker in str(exc).lower() for marker in _CONTEXT_MARKERS):
                raise LLMInputTooLarge("AI gateway input exceeded the model context") from exc
            raise LLMTransientError("AI gateway stream failed") from exc


_POLICY_CACHE: dict[str, tuple[float, dict]] = {}


def alias_policy(path: str | None) -> dict:
    """Return the gateway's alias policy as ``{"revision", "aliases"}``.

    An unreadable policy yields null fields. That never blocks a call, but a
    snapshot recorded without a known alias target is not reused later.
    """
    empty = {"revision": None, "aliases": {}}
    if not path:
        return empty
    file = Path(path).expanduser()
    try:
        mtime = file.stat().st_mtime
        cached = _POLICY_CACHE.get(str(file))
        if cached is not None and cached[0] == mtime:
            return cached[1]
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("AI gateway alias policy is unreadable: %s", file)
        return empty
    aliases = raw.get("aliases") if isinstance(raw, dict) else None
    policy = {
        "revision": raw.get("revision") if isinstance(raw, dict) else None,
        "aliases": {str(key): str(value) for key, value in (aliases or {}).items() if value}
        if isinstance(aliases, dict)
        else {},
    }
    _POLICY_CACHE[str(file)] = (mtime, policy)
    return policy
