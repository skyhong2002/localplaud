"""ASR provider registry + selection with fallback.

Providers are looked up lazily by name so that importing localplaud never
requires every optional ASR dependency (torch, mlx, cloud SDKs) to be
installed. Only the provider you actually select is imported.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..config import AsrConfig
from ..providers.failures import allows_provider_fallback
from .base import AsrProvider, AsrUnavailable, Transcript

log = logging.getLogger(__name__)

# name -> factory(AsrConfig) -> AsrProvider. Factories import their heavy deps
# inside the function body so unused providers cost nothing.
_FACTORIES: dict[str, Callable[[AsrConfig], AsrProvider]] = {}
_BUILTINS_LOADED = False


def register(name: str) -> Callable[[Callable[[AsrConfig], AsrProvider]], Callable]:
    def deco(factory: Callable[[AsrConfig], AsrProvider]):
        _FACTORIES[name] = factory
        return factory

    return deco


def _load_builtin_factories() -> None:
    # Import provider modules for their @register side effects. Import errors
    # (missing optional deps) are tolerated — that provider just won't be
    # available, which the fallback logic handles.
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return

    from importlib import import_module

    for mod in (
        "faster_whisper_provider",
        "whispercpp_provider",
        "mlx_provider",
        "openai_provider",
        "deepgram_provider",
        "assemblyai_provider",
        "remote_worker_provider",
    ):
        try:
            import_module(f"{__package__}.{mod}")
        except Exception as exc:  # noqa: BLE001 - optional deps may be absent
            log.debug("ASR provider module %s not loaded: %s", mod, exc)
    _BUILTINS_LOADED = True


def build_provider(name: str, cfg: AsrConfig) -> AsrProvider:
    # A provider module can be imported directly before the first registry
    # lookup. That makes the registry non-empty but does not mean every built-in
    # factory has been loaded (doctor used to report mlx-whisper as unknown in
    # precisely this state).
    _load_builtin_factories()
    if name not in _FACTORIES:
        raise ValueError(f"unknown ASR provider: {name!r}")
    return _FACTORIES[name](cfg)


def transcribe_with_fallback(audio_path, cfg: AsrConfig) -> Transcript:
    """Try the configured provider, then each fallback in order, skipping any
    that report themselves unavailable or raise :class:`AsrUnavailable`."""
    fallback_routes = getattr(cfg, "fallback_routes", []) or []
    routes = [{"provider": cfg.provider}, *fallback_routes]
    if not fallback_routes:
        routes.extend({"provider": item} for item in cfg.fallback if item != cfg.provider)
    order = [route["provider"] for route in routes]
    last_err: Exception | None = None
    for route in routes:
        name = route["provider"]
        candidate_cfg = _candidate_config(cfg, route)
        try:
            provider = build_provider(name, candidate_cfg)
        except AsrUnavailable as exc:
            log.warning("ASR provider %s could not be built: %s", name, exc)
            last_err = exc
            continue
        try:
            log.info("Transcribing with ASR provider %s", name)
            return provider.transcribe(audio_path, language=candidate_cfg.language)
        except Exception as exc:
            if not allows_provider_fallback(exc):
                raise
            log.warning("ASR provider %s unavailable or retryable mid-run: %s", name, exc)
            last_err = exc
            continue
    raise AsrUnavailable(
        f"no ASR provider could transcribe (tried {order}): {last_err}"
    )


def _candidate_config(cfg: AsrConfig, route: dict) -> AsrConfig:
    candidate = cfg.model_copy(deep=True)
    name = route["provider"]
    candidate.provider = name
    candidate.fallback = []
    candidate.fallback_routes = []
    target = getattr(candidate, name.replace("-", "_"), None)
    if target is None:
        return candidate
    if route.get("model") is not None and hasattr(target, "model"):
        target.model = route["model"]
    for key, value in (route.get("options") or {}).items():
        if key == "language":
            candidate.language = value
        elif hasattr(target, key):
            setattr(target, key, value)
    for key in ("base_url", "headers", "timeout_seconds", "max_input_bytes"):
        if key in route and hasattr(target, key):
            setattr(target, key, route[key])
    if route.get("secret_ref") and hasattr(target, "api_key"):
        from ..providers.routing import _resolve_secret_ref

        value = _resolve_secret_ref(route["secret_ref"])
        if not value:
            raise ValueError(f"secret reference {route['secret_ref']!r} is unavailable")
        target.api_key = value
    return candidate
