"""OpenAI provider request compatibility and reasoning-model options."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from localplaud.config import OpenAILlmConfig, Settings
from localplaud.llm.openai_llm import OpenAILLM
from localplaud.worker.pipeline import _settings_for_stage


def _fake_openai(monkeypatch):
    calls: list[dict] = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    module = SimpleNamespace(OpenAI=lambda **_kwargs: client)
    monkeypatch.setitem(sys.modules, "openai", module)
    return calls


def test_openai_reasoning_request_uses_completion_budget_without_temperature(monkeypatch):
    calls = _fake_openai(monkeypatch)
    provider = OpenAILLM(
        OpenAILlmConfig(
            api_key="test-key",
            model="gpt-5.4",
            reasoning_effort="medium",
        )
    )
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }

    assert provider.complete("Return JSON", max_tokens=321, json_schema=schema)

    request = calls[0]
    assert request["model"] == "gpt-5.4"
    assert request["reasoning_effort"] == "medium"
    assert request["max_completion_tokens"] == 321
    assert "temperature" not in request
    assert "max_tokens" not in request
    assert request["response_format"]["json_schema"]["schema"] == schema


def test_openai_compatible_request_preserves_legacy_sampling_parameters(monkeypatch):
    calls = _fake_openai(monkeypatch)
    provider = OpenAILLM(
        OpenAILlmConfig(
            api_key="test-key",
            base_url="https://compatible.example/v1",
            model="compatible-model",
        )
    )

    provider.complete("Hello", temperature=0.2, max_tokens=123)

    request = calls[0]
    assert request["temperature"] == 0.2
    assert request["max_tokens"] == 123
    assert "reasoning_effort" not in request
    assert "max_completion_tokens" not in request


def test_profile_options_project_gpt_5_4_medium_without_mutating_base_settings():
    settings = Settings(llm={"provider": "ollama"})
    snapshot = {
        "stages": {
            "summarize": {
                "connection": "llm:openai",
                "provider_type": "openai",
                "model": "gpt-5.4",
                "configuration": {},
                "options": {"reasoning_effort": "medium"},
            }
        }
    }

    resolved = _settings_for_stage(settings, snapshot, "summarize")

    assert resolved.llm.provider == "openai"
    assert resolved.llm.openai.model == "gpt-5.4"
    assert resolved.llm.openai.reasoning_effort == "medium"
    assert settings.llm.provider == "ollama"
    assert settings.llm.openai.reasoning_effort is None
