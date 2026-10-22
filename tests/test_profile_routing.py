import os

import pytest

from localplaud.config import Settings
from localplaud.providers.routing import UnsupportedRoute, settings_for_route


def test_route_changes_only_the_target_stage_settings():
    settings = Settings()
    original_provider = settings.asr.provider
    routed = settings_for_route(settings, "transcribe", {"route": {
        "provider": "mlx-whisper", "model": "custom/turbo", "fallback": [],
        "options": {"language": "zh"},
    }})
    assert routed.asr.provider == "mlx-whisper"
    assert routed.asr.mlx_whisper.model == "custom/turbo"
    assert routed.asr.language == "zh"
    assert settings.asr.provider == original_provider


def test_partial_openai_capability_does_not_imply_other_shapes():
    settings = Settings()
    text = settings_for_route(settings, "summarize", {
        "route": {"provider": "openai", "model": "text-model"}
    })
    assert text.llm.openai.model == "text-model"
    with pytest.raises(UnsupportedRoute, match="ASR provider"):
        settings_for_route(settings, "transcribe", {
            "route": {"provider": "openai-text", "model": "text-model"}
        })


def test_env_secret_reference_resolves_without_entering_snapshot(monkeypatch):
    settings = Settings()
    monkeypatch.setenv("LOCALPLAUD_TEST_OPENAI_KEY", "resolved-secret")
    snapshot = {"route": {"provider": "openai", "model": "text-model",
                          "secret_ref": "env:LOCALPLAUD_TEST_OPENAI_KEY"}}
    routed = settings_for_route(settings, "summarize", snapshot)
    assert routed.llm.openai.api_key == "resolved-secret"
    assert "resolved-secret" not in str(snapshot)


def test_dotenv_secret_reference_resolves_without_exporting_or_snapshot_leak(
    monkeypatch, tmp_path,
):
    from localplaud.providers.routing import _resolve_secret_ref

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOCALPLAUD_TEST_DOTENV_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "LOCALPLAUD_TEST_DOTENV_KEY=dotenv-secret\n", encoding="utf-8"
    )
    assert _resolve_secret_ref("env:LOCALPLAUD_TEST_DOTENV_KEY") == "dotenv-secret"
    assert "LOCALPLAUD_TEST_DOTENV_KEY" not in os.environ
