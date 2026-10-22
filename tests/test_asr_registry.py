"""ASR registry: registration, selection, and fallback — no optional deps."""

import builtins
from pathlib import Path

import pytest

from localplaud.asr import registry
from localplaud.asr.base import AsrUnavailable, Segment, Transcript
from localplaud.config import AsrConfig
from localplaud.providers.failures import RetryableProviderError

AUDIO = Path("/nonexistent/audio.opus")  # dummies never touch the file


def _cfg(provider: str, fallback: list[str] | None = None) -> AsrConfig:
    # "dummy"/"brokenprimary" are not in the AsrProviderName Literal, so
    # bypass validation; model_construct still fills the other defaults.
    return AsrConfig.model_construct(provider=provider, fallback=fallback or [])


@pytest.fixture()
def canned() -> Transcript:
    return Transcript(
        segments=[Segment(text="canned result", start=0.0, end=1.0)],
        language="en",
        provider="dummy",
    )


@pytest.fixture()
def dummy_providers(canned):
    """Register a working 'dummy' and an unavailable 'brokenprimary'."""

    class Dummy:
        name = "dummy"

        def available(self) -> bool:
            return True

        def transcribe(self, audio_path, language="auto") -> Transcript:
            return canned

    class BrokenPrimary:
        name = "brokenprimary"

        def available(self) -> bool:
            return False

        def transcribe(self, audio_path, language="auto") -> Transcript:
            raise AsrUnavailable("provider runtime is unavailable")

    registry.register("dummy")(lambda cfg: Dummy())
    registry.register("brokenprimary")(lambda cfg: BrokenPrimary())
    try:
        yield
    finally:
        registry._FACTORIES.pop("dummy", None)
        registry._FACTORIES.pop("brokenprimary", None)


def test_registered_provider_transcribes(dummy_providers, canned):
    result = registry.transcribe_with_fallback(AUDIO, _cfg("dummy"))
    assert result is canned
    assert result.text == "canned result"


def test_build_provider_returns_registered_factory_product(dummy_providers):
    provider = registry.build_provider("dummy", _cfg("dummy"))
    assert provider.name == "dummy"
    assert provider.available() is True


def test_partial_registry_still_loads_builtins(monkeypatch, dummy_providers):
    calls = []

    def load_all():
        calls.append(True)
        registry._BUILTINS_LOADED = True

    monkeypatch.setattr(registry, "_BUILTINS_LOADED", False)
    monkeypatch.setattr(registry, "_load_builtin_factories", load_all)
    provider = registry.build_provider("dummy", _cfg("dummy"))
    assert provider.name == "dummy"
    assert calls == [True]


def test_fallback_when_primary_unavailable(dummy_providers, canned):
    cfg = _cfg("brokenprimary", fallback=["dummy"])
    result = registry.transcribe_with_fallback(AUDIO, cfg)
    assert result is canned


@pytest.mark.parametrize("failure", [
    ValueError("invalid input"),
    AssertionError("programming bug"),
    PermissionError("authentication failed"),
    RuntimeError("non-retryable provider failure"),
])
def test_non_retryable_failures_never_fall_back(dummy_providers, failure):
    class Failed:
        def available(self):
            return True

        def transcribe(self, *args, **kwargs):
            raise failure

    registry._FACTORIES["failed"] = lambda cfg: Failed()
    try:
        with pytest.raises(type(failure), match=str(failure)):
            registry.transcribe_with_fallback(AUDIO, _cfg("failed", ["dummy"]))
    finally:
        registry._FACTORIES.pop("failed", None)


def test_retryable_failure_uses_explicit_fallback(dummy_providers, canned):
    class Retryable:
        def available(self):
            return True

        def transcribe(self, *args, **kwargs):
            raise RetryableProviderError("temporary outage")

    registry._FACTORIES["retryable"] = lambda cfg: Retryable()
    try:
        assert registry.transcribe_with_fallback(
            AUDIO, _cfg("retryable", ["dummy"])
        ) is canned
    finally:
        registry._FACTORIES.pop("retryable", None)


def test_no_provider_available_raises(dummy_providers):
    cfg = _cfg("brokenprimary", fallback=["brokenprimary"])
    with pytest.raises(AsrUnavailable):
        registry.transcribe_with_fallback(AUDIO, cfg)


def test_fallback_uses_its_own_resolved_model_and_options(canned):
    seen = {}

    class Broken:
        def available(self):
            return False

        def transcribe(self, *args, **kwargs):
            raise AsrUnavailable("provider runtime is unavailable")

    class Fallback:
        def __init__(self, cfg):
            seen["model"] = cfg.openai.model
            seen["base_url"] = cfg.openai.base_url

        def available(self):
            return True

        def transcribe(self, path, language="auto"):
            seen["language"] = language
            return canned

    registry._FACTORIES["faster-whisper"] = lambda cfg: Broken()
    registry._FACTORIES["openai"] = lambda cfg: Fallback(cfg)
    cfg = AsrConfig(provider="faster-whisper", fallback=["openai"], fallback_routes=[{
        "provider": "openai", "model": "fallback-audio",
        "base_url": "https://example.com/v1", "options": {"language": "zh"},
    }])
    try:
        assert registry.transcribe_with_fallback(AUDIO, cfg) is canned
        assert seen == {"model": "fallback-audio", "base_url": "https://example.com/v1",
                        "language": "zh"}
    finally:
        registry._FACTORIES.pop("faster-whisper", None)
        registry._FACTORIES.pop("openai", None)


def test_mlx_health_explains_dependency_import_failure(monkeypatch):
    from localplaud.asr.mlx_provider import MlxWhisperProvider

    real_import = builtins.__import__

    def fail_mlx_import(name, *args, **kwargs):
        if name == "mlx_whisper":
            raise ImportError("Numba needs NumPy 2.4 or less")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_mlx_import)
    ok, detail = MlxWhisperProvider(AsrConfig()).health()
    assert ok is False
    assert "Numba needs NumPy 2.4 or less" in detail
