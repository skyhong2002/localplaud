"""Fail-closed daily free-pool guard for calls to the real OpenAI API."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from datetime import datetime
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

if TYPE_CHECKING:
    from .config import Settings

_USAGE_BASE_URL = "https://api.openai.com/v1/organization/usage"
_USAGE_ENDPOINTS = ("completions", "embeddings", "audio_transcriptions")
_CACHE_TTL_SECONDS = 60.0
_usage_cache: dict[tuple[str, int, tuple[str, ...]], tuple[float, dict[str, int]]] = {}
_cache_lock = threading.Lock()


class OpenAIBudgetBlocked(Exception):
    """Raised before OpenAI egress when free-pool safety cannot be guaranteed."""


def is_real_openai_base_url(base_url: str | None) -> bool:
    """Return whether an SDK adapter points at OpenAI rather than a relay."""
    if not base_url or not base_url.strip():
        return True
    value = base_url.strip()
    parsed = urlsplit(value if "://" in value else f"//{value}")
    return (parsed.hostname or "").casefold() == "api.openai.com"


def assert_openai_free_pool(
    settings: Settings, *, model: str, projected_tokens: int
) -> None:
    """Refuse a real OpenAI call unless its daily free pool is verifiably safe."""
    config = settings.openai_budget
    if not config.enabled:
        return

    totals = _pool_totals(settings)
    pool = _model_pool(model, config.mini_model_patterns)
    used = totals[pool]
    limit = config.mini_pool_limit if pool == "mini" else config.high_pool_limit
    threshold = limit * (1 - config.safety_margin_fraction)
    if used + max(projected_tokens, 0) > threshold:
        raise OpenAIBudgetBlocked(
            f"OpenAI free daily pool exhausted: {pool} pool {used:,}/{limit:,} tokens "
            f"used today ({config.timezone}). Processing stopped to avoid charges; retry "
            "after the daily reset or adjust openai_budget limits."
        )


def openai_free_pool_health(settings: Settings) -> tuple[bool, str]:
    """Return a compact provider-health detail without exposing credentials."""
    config = settings.openai_budget
    if not config.enabled:
        return True, ""
    try:
        totals = _pool_totals(settings)
    except OpenAIBudgetBlocked as exc:
        return False, str(exc)
    high_threshold = config.high_pool_limit * (1 - config.safety_margin_fraction)
    mini_threshold = config.mini_pool_limit * (1 - config.safety_margin_fraction)
    detail = (
        f"free pool: high {totals['high']:,}/{config.high_pool_limit:,} · "
        f"mini {totals['mini']:,}/{config.mini_pool_limit:,} today"
    )
    return totals["high"] <= high_threshold and totals["mini"] <= mini_threshold, detail


def _pool_totals(settings: Settings) -> dict[str, int]:
    config = settings.openai_budget
    admin_key = _resolve_admin_key(config.admin_key)
    try:
        timezone = ZoneInfo(config.timezone)
        now = datetime.now(timezone)
        day_start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    except Exception as exc:
        raise OpenAIBudgetBlocked(
            "OpenAI budget gate could not verify the free pool; refusing to call OpenAI "
            f"to avoid charges: invalid timezone {config.timezone!r}: {exc}"
        ) from exc

    patterns = tuple(pattern.casefold() for pattern in config.mini_model_patterns)
    key_fingerprint = hashlib.sha256(admin_key.encode()).hexdigest()
    cache_key = (key_fingerprint, day_start, patterns)
    now_monotonic = time.monotonic()

    # Keep the lock while refreshing so a burst produces one usage-API fetch.
    with _cache_lock:
        cached = _usage_cache.get(cache_key)
        if cached is not None and now_monotonic - cached[0] < _CACHE_TTL_SECONDS:
            return dict(cached[1])
        try:
            totals = _fetch_pool_totals(
                admin_key=admin_key,
                start_time=day_start,
                mini_model_patterns=config.mini_model_patterns,
            )
        except Exception as exc:
            raise OpenAIBudgetBlocked(
                "OpenAI budget gate could not verify the free pool; refusing to call "
                f"OpenAI to avoid charges: {exc}"
            ) from exc
        _usage_cache[cache_key] = (time.monotonic(), totals)
        return dict(totals)


def _resolve_admin_key(reference: str) -> str:
    env_name = reference.removeprefix("env:") if reference.startswith("env:") else ""
    value = os.environ.get(env_name) if env_name else None
    if value:
        return value
    missing_name = env_name or "OPENAI_ADMIN_KEY"
    raise OpenAIBudgetBlocked(
        f"OpenAI budget gate is enabled but {missing_name} is not set. Configure "
        f'openai_budget.admin_key = "env:{missing_name}" and set that environment variable.'
    )


def _fetch_pool_totals(
    *, admin_key: str, start_time: int, mini_model_patterns: list[str]
) -> dict[str, int]:
    totals = {"high": 0, "mini": 0}
    headers = {"Authorization": f"Bearer {admin_key}"}
    with httpx.Client(timeout=30.0, follow_redirects=False, headers=headers) as client:
        for endpoint in _USAGE_ENDPOINTS:
            page: str | None = None
            while True:
                params: dict[str, str | int] = {
                    "start_time": start_time,
                    "group_by": "model",
                    "limit": 31,
                }
                if page is not None:
                    params["page"] = page
                response = client.get(f"{_USAGE_BASE_URL}/{endpoint}", params=params)
                if response.status_code == 404:
                    break
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ValueError(f"invalid {endpoint} usage response")
                for bucket in payload["data"]:
                    if not isinstance(bucket, dict) or not isinstance(bucket.get("results"), list):
                        raise ValueError(f"invalid {endpoint} usage bucket")
                    for result in bucket["results"]:
                        if not isinstance(result, dict):
                            raise ValueError(f"invalid {endpoint} usage result")
                        model = result.get("model")
                        model_name = model if isinstance(model, str) else ""
                        pool = _model_pool(model_name, mini_model_patterns)
                        totals[pool] += _token_count(result, "input_tokens")
                        totals[pool] += _token_count(result, "output_tokens")
                if not payload.get("has_more", False):
                    break
                next_page = payload.get("next_page")
                if not isinstance(next_page, str) or not next_page:
                    raise ValueError(f"invalid {endpoint} usage pagination")
                page = next_page
    return totals


def _token_count(result: dict, field: str) -> int:
    value = result.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid usage token count for {field}")
    return value


def _model_pool(model: str, mini_model_patterns: list[str]) -> str:
    normalized = model.casefold()
    if any(fnmatchcase(normalized, pattern.casefold()) for pattern in mini_model_patterns):
        return "mini"
    return "high"
