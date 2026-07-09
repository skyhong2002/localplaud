"""ASR provider registry + selection with fallback.

Providers are looked up lazily by name so that importing localplaud never
requires every optional ASR dependency (torch, mlx, cloud SDKs) to be
installed. Only the provider you actually select is imported.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ..config import AsrConfig
from .base import AsrProvider, AsrUnavailable, Transcript

log = logging.getLogger(__name__)

# name -> factory(AsrConfig) -> AsrProvider. Factories import their heavy deps
# inside the function body so unused providers cost nothing.
_FACTORIES: dict[str, Callable[[AsrConfig], AsrProvider]] = {}


def register(name: str) -> Callable[[Callable[[AsrConfig], AsrProvider]], Callable]:
    def deco(factory: Callable[[AsrConfig], AsrProvider]):
        _FACTORIES[name] = factory
        return factory

    return deco


def _load_builtin_factories() -> None:
    # Import provider modules for their @register side effects. Import errors
    # (missing optional deps) are tolerated — that provider just won't be
    # available, which the fallback logic handles.
    from importlib import import_module

    for mod in (
        "faster_whisper_provider",
        "whispercpp_provider",
        "mlx_provider",
        "openai_provider",
        "deepgram_provider",
        "assemblyai_provider",
    ):
        try:
            import_module(f"{__package__}.{mod}")
        except Exception as exc:  # noqa: BLE001 - optional deps may be absent
            log.debug("ASR provider module %s not loaded: %s", mod, exc)


def build_provider(name: str, cfg: AsrConfig) -> AsrProvider:
    if not _FACTORIES:
        _load_builtin_factories()
    if name not in _FACTORIES:
        raise AsrUnavailable(f"unknown ASR provider: {name!r}")
    return _FACTORIES[name](cfg)


def transcribe_with_fallback(audio_path, cfg: AsrConfig) -> Transcript:
    """Try the configured provider, then each fallback in order, skipping any
    that report themselves unavailable or raise :class:`AsrUnavailable`."""
    order = [cfg.provider, *[p for p in cfg.fallback if p != cfg.provider]]
    last_err: Exception | None = None
    for name in order:
        try:
            provider = build_provider(name, cfg)
        except AsrUnavailable as exc:
            log.warning("ASR provider %s could not be built: %s", name, exc)
            last_err = exc
            continue
        if not provider.available():
            log.warning("ASR provider %s is not available here; trying next", name)
            last_err = AsrUnavailable(f"{name} unavailable")
            continue
        try:
            log.info("Transcribing with ASR provider %s", name)
            return provider.transcribe(audio_path, language=cfg.language)
        except AsrUnavailable as exc:
            log.warning("ASR provider %s unavailable mid-run: %s", name, exc)
            last_err = exc
            continue
    raise AsrUnavailable(
        f"no ASR provider could transcribe (tried {order}): {last_err}"
    )
