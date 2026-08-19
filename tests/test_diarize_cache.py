"""Offline reuse of an already-authorized pyannote pipeline cache."""

from __future__ import annotations

from types import SimpleNamespace

from localplaud.config import DiarizeConfig
from localplaud.worker import diarize as diarize_module


def _fake_pyannote(monkeypatch, *, pipeline=None):
    audio = SimpleNamespace(Pipeline=pipeline) if pipeline is not None else SimpleNamespace()
    monkeypatch.setitem(__import__("sys").modules, "pyannote", SimpleNamespace(audio=audio))
    monkeypatch.setitem(__import__("sys").modules, "pyannote.audio", audio)


def test_health_accepts_complete_cached_pipeline_without_token(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("pipeline: {}", encoding="utf-8")
    monkeypatch.setattr(diarize_module, "_cached_pipeline_config", lambda _model: config)
    monkeypatch.setattr(diarize_module, "_resolve_device", lambda _cfg: (object(), "cpu"))
    _fake_pyannote(monkeypatch)

    ok, detail = diarize_module.health(DiarizeConfig(hf_token=None, device="cpu"))

    assert ok is True
    assert "cached offline" in detail


def test_cached_pipeline_rejects_incomplete_snapshot(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("pipeline: {}", encoding="utf-8")
    monkeypatch.setitem(
        __import__("sys").modules,
        "huggingface_hub",
        SimpleNamespace(try_to_load_from_cache=lambda *_args: str(config)),
    )

    assert diarize_module._cached_pipeline_config("pyannote/test") is None


def test_load_pipeline_uses_cached_config_without_token(monkeypatch, tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("pipeline: {}", encoding="utf-8")
    calls = []
    pipeline = SimpleNamespace(to=lambda device: calls.append(("to", str(device))))

    class FakePipeline:
        @classmethod
        def from_pretrained(cls, checkpoint, token=None):
            calls.append(("load", checkpoint, token))
            return pipeline

    monkeypatch.setattr(diarize_module, "_cached_pipeline_config", lambda _model: config)
    monkeypatch.setattr(
        diarize_module,
        "_resolve_device",
        lambda _cfg: (SimpleNamespace(device=lambda value: value), "cpu"),
    )
    _fake_pyannote(monkeypatch, pipeline=FakePipeline)

    assert diarize_module._load_pipeline(DiarizeConfig(hf_token=None, device="cpu")) is pipeline
    assert calls == [("load", config, None), ("to", "cpu")]


def test_load_pipeline_still_requires_auth_when_cache_is_absent(monkeypatch):
    monkeypatch.setattr(diarize_module, "_cached_pipeline_config", lambda _model: None)
    _fake_pyannote(monkeypatch, pipeline=object())

    try:
        diarize_module._load_pipeline(DiarizeConfig(hf_token=None))
    except diarize_module.DiarizationUnavailable as exc:
        assert "not cached locally" in str(exc)
    else:  # pragma: no cover - explicit assertion keeps the failure readable
        raise AssertionError("missing cache and token must fail closed")
