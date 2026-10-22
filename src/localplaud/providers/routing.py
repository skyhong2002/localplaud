"""Apply an immutable resolved route to a per-stage copy of Settings."""

from __future__ import annotations

import os
import subprocess
from typing import Any

from ..config import Settings
from ..db.models import StageName


class UnsupportedRoute(ValueError):
    pass


def _apply_model_options(target, route: dict[str, Any]) -> None:
    if route.get("model") is not None and hasattr(target, "model"):
        target.model = route["model"]
    for key, value in (route.get("options") or {}).items():
        if hasattr(target, key):
            setattr(target, key, value)
    for key in ("base_url", "headers", "timeout_seconds", "poll_interval_seconds",
                "max_input_bytes",
                "max_input_chars", "dim"):
        if key in route and hasattr(target, key):
            setattr(target, key, route[key])
    secret_ref = route.get("secret_ref")
    if secret_ref and (hasattr(target, "api_key") or hasattr(target, "token")):
        value = _resolve_secret_ref(secret_ref)
        if not value:
            raise UnsupportedRoute(f"secret reference {secret_ref!r} is not available")
        if hasattr(target, "api_key"):
            target.api_key = value
        else:
            target.token = value


def _resolve_secret_ref(secret_ref: str) -> str | None:
    if secret_ref.startswith("env:"):
        name = secret_ref.removeprefix("env:")
        value = os.getenv(name)
        if value is not None:
            return value
        # BaseSettings reads .env without exporting it into os.environ. Durable
        # provider connections must use the same secret source or a launchd
        # service can appear configured while failing at dispatch time.
        from dotenv import dotenv_values

        dotenv_value = dotenv_values(".env").get(name)
        return str(dotenv_value) if dotenv_value is not None else None
    if secret_ref.startswith("keychain:"):
        service = secret_ref.removeprefix("keychain:")
        if not service:
            raise UnsupportedRoute("keychain secret reference requires a service name")
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UnsupportedRoute("macOS Keychain lookup failed") from exc
        return result.stdout.strip() if result.returncode == 0 else None
    raise UnsupportedRoute("secret_ref must use env: or keychain:")


def settings_for_route(settings: Settings, stage: StageName | str,
                       snapshot: dict[str, Any]) -> Settings:
    routed = settings.model_copy(deep=True)
    stage_name = stage.value if isinstance(stage, StageName) else stage
    route = snapshot.get("route") or {}
    provider = route.get("provider")
    if not provider:
        raise UnsupportedRoute(f"resolved {stage_name} route has no provider")

    if stage_name == "transcribe":
        supported = {"faster-whisper", "whispercpp", "mlx-whisper", "openai",
                     "deepgram", "assemblyai", "remote-worker"}
        if provider not in supported:
            raise UnsupportedRoute(f"ASR provider {provider!r} is not installed in this runtime")
        routed.asr.provider = provider
        routed.asr.fallback = [item["provider"] for item in route.get("fallback", [])]
        routed.asr.fallback_routes = route.get("fallback", [])
        options = route.get("options") or {}
        if "language" in options:
            routed.asr.language = options["language"]
        target = getattr(routed.asr, provider.replace("-", "_"))
        _apply_model_options(target, route)
    elif stage_name == "align":
        if provider not in {"whisper-timestamps", "whisperx", "remote-worker", "none"}:
            raise UnsupportedRoute(f"alignment provider {provider!r} is unsupported")
        routed.align.provider = provider
        _apply_model_options(routed.align, route)
    elif stage_name == "diarize":
        if provider not in {"pyannote", "remote-worker", "none"}:
            raise UnsupportedRoute(f"diarization provider {provider!r} is unsupported")
        routed.diarize.provider = provider
        _apply_model_options(routed.diarize, route)
    elif stage_name == "correct":
        if provider not in {"local-rules", "remote-worker", "ollama", "openai",
                            "anthropic", "codex-local"}:
            raise UnsupportedRoute(f"correction provider {provider!r} is unsupported")
        routed.llm.correction_provider = provider
        if provider != "local-rules":
            routed.llm.provider = provider
            target = getattr(routed.llm, provider.replace("-", "_"))
            _apply_model_options(target, route)
            if provider == "remote-worker":
                routed.llm.remote_worker.capability = "correction"
        else:
            # Marker used by the correction dispatcher; local rules do not call
            # the selected text model.
            routed.llm.provider = "ollama"
    elif stage_name in {"summarize", "mind_map", "ask"}:
        if provider not in {"ollama", "openai", "anthropic", "codex-local", "remote-worker"}:
            raise UnsupportedRoute(f"text provider {provider!r} is unsupported")
        routed.llm.provider = provider
        _apply_model_options(getattr(routed.llm, provider.replace("-", "_")), route)
        if provider == "remote-worker":
            routed.llm.remote_worker.capability = stage_name
    elif stage_name == "index":
        if provider not in {"local", "openai", "ollama", "remote-worker"}:
            raise UnsupportedRoute(f"embedding provider {provider!r} is unsupported")
        routed.embeddings.provider = provider
        _apply_model_options(getattr(routed.embeddings, provider), route)
    return routed
