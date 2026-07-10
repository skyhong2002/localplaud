"""ASR registry: registration, selection, and fallback — no optional deps."""

import builtins
from pathlib import Path

import pytest

from localplaud.asr import registry
from localplaud.asr.base import AsrUnavailable, Segment, Transcript
from localplaud.config import AsrConfig

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
            raise AssertionError("must never be called: available() is False")

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


def test_fallback_when_primary_unavailable(dummy_providers, canned):
    cfg = _cfg("brokenprimary", fallback=["dummy"])
    result = registry.transcribe_with_fallback(AUDIO, cfg)
    assert result is canned


def test_no_provider_available_raises(dummy_providers):
    cfg = _cfg("brokenprimary", fallback=["brokenprimary"])
    with pytest.raises(AsrUnavailable):
        registry.transcribe_with_fallback(AUDIO, cfg)


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
