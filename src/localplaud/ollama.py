"""Shared Ollama discovery helpers for model-aware provider health checks."""

from __future__ import annotations


def _model_names(payload: dict) -> set[str]:
    names: set[str] = set()
    for item in payload.get("models") or []:
        if not isinstance(item, dict):
            continue
        for key in ("name", "model"):
            value = item.get(key)
            if isinstance(value, str) and value:
                names.add(value)
    return names


def _matches_model(configured: str, installed: set[str]) -> bool:
    if configured in installed:
        return True
    if ":" not in configured and f"{configured}:latest" in installed:
        return True
    return False


def model_health(host: str, model: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    """Return daemon + configured-model health without running inference."""
    import httpx

    try:
        response = httpx.get(f"{host.rstrip('/')}/api/tags", timeout=timeout)
        response.raise_for_status()
        installed = _model_names(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        return False, f"cannot reach Ollama: {exc}"
    if not _matches_model(model, installed):
        return False, f"model {model!r} is not installed; run `ollama pull {model}`"
    return True, f"model {model} is installed"


def response_error(response) -> str:
    """Extract a bounded Ollama error string from JSON or plain text."""
    try:
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            return str(payload["error"])[:500]
    except ValueError:
        pass
    return response.text[:500]
