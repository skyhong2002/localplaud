"""OpenAI provider request compatibility and reasoning-model options."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from localplaud.config import OpenAILlmConfig, Settings
from localplaud.llm.base import LLMError
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
            base_url="https://relay.example.test/v1",
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


def _fake_responses(monkeypatch, events):
    calls: list[dict] = []

    class Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return iter(events)

    client = SimpleNamespace(responses=Responses())
    module = SimpleNamespace(OpenAI=lambda **_kwargs: client)
    monkeypatch.setitem(sys.modules, "openai", module)
    return calls


def _delta(text):
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def test_responses_mode_streams_reasoning_models_with_structured_output(monkeypatch):
    """Relays terminate a request that stays silent while the model reasons,
    so a reasoning endpoint must stream the typed event feed."""
    calls = _fake_responses(
        monkeypatch,
        [
            SimpleNamespace(type="response.created"),
            SimpleNamespace(type="response.reasoning_summary_text.delta", delta="thinking"),
            _delta('{"ok"'),
            _delta(":true}"),
            SimpleNamespace(type="response.completed"),
        ],
    )
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    provider = OpenAILLM(
        OpenAILlmConfig(
            api_key="k",
            base_url="http://127.0.0.1:8317/v1",
            model="gpt-5.6-luna",
            reasoning_effort="xhigh",
            api_mode="responses",
        )
    )

    assert provider.complete("polish", system="sys", max_tokens=999, json_schema=schema) == (
        '{"ok":true}'
    )

    request = calls[0]
    assert request["stream"] is True
    assert request["reasoning"] == {"effort": "xhigh"}
    assert request["max_output_tokens"] == 999
    assert request["input"][0] == {"role": "system", "content": "sys"}
    assert request["text"]["format"]["schema"] == schema
    assert request["text"]["format"]["strict"] is True
    # Reasoning models must not receive sampling temperature.
    assert "temperature" not in request
    assert "messages" not in request


def test_responses_mode_rejects_a_truncated_response(monkeypatch):
    """A truncated correction silently drops transcript; it is not a result."""
    _fake_responses(
        monkeypatch,
        [
            _delta('{"segments":['),
            SimpleNamespace(
                type="response.incomplete",
                response=SimpleNamespace(
                    incomplete_details=SimpleNamespace(reason="max_output_tokens")
                ),
            ),
        ],
    )
    provider = OpenAILLM(
        OpenAILlmConfig(
            api_key="k",
            base_url="https://relay.example.test/v1",
            model="gpt-5.6-luna",
            api_mode="responses",
        )
    )

    with pytest.raises(LLMError, match="incomplete \\(max_output_tokens\\)"):
        provider.complete("polish")


def test_responses_mode_rejects_a_stream_without_terminal_completion(monkeypatch):
    _fake_responses(monkeypatch, [_delta("partial output")])
    provider = OpenAILLM(
        OpenAILlmConfig(api_key="k", model="gpt-5.6-luna", api_mode="responses")
    )

    with pytest.raises(LLMError, match="ended before completion"):
        provider.complete("polish")


def test_responses_mode_surfaces_stream_errors(monkeypatch):
    _fake_responses(
        monkeypatch,
        [SimpleNamespace(type="error", error=SimpleNamespace(message="relay disconnected"))],
    )
    provider = OpenAILLM(
        OpenAILlmConfig(api_key="k", model="gpt-5.6-luna", api_mode="responses")
    )

    with pytest.raises(LLMError, match="relay disconnected"):
        provider.complete("polish")


def test_chat_mode_remains_the_default_and_untouched(monkeypatch):
    calls = _fake_openai(monkeypatch)
    provider = OpenAILLM(
        OpenAILlmConfig(
            api_key="k", base_url="https://relay.example.test/v1", model="gpt-4o-mini"
        )
    )

    assert provider.cfg.api_mode == "chat"
    provider.complete("hi", max_tokens=50)

    assert "messages" in calls[0]
    assert "input" not in calls[0]


def test_openai_endpoints_carry_their_own_polish_chunk_budget(monkeypatch):
    """A relay that cuts long silent requests needs smaller correction chunks
    than the global default, without shrinking them for every other provider."""
    from localplaud.asr.base import Segment, Transcript
    from localplaud.worker.polish import polish_transcript

    provider = OpenAILLM(
        OpenAILlmConfig(api_key="k", model="gpt-5.6-luna", polish_chunk_chars=3_000)
    )
    assert provider.polish_chunk_chars == 3_000
    assert provider.model == "gpt-5.6-luna"

    seen: list[int] = []

    def fake_complete(prompt, **_kwargs):
        import json

        request = json.loads(prompt)
        seen.append(len(request["target_segments"]))
        return json.dumps(
            {"segments": [{"id": item["id"], "text": item["text"]}
                          for item in request["target_segments"]]}
        )

    monkeypatch.setattr(provider, "complete", fake_complete)
    monkeypatch.setattr(provider, "available", lambda: True)
    monkeypatch.setattr("localplaud.worker.polish.build_llm", lambda _cfg: provider)

    settings = Settings()
    settings.pipeline.polish_chunk_chars = 48_000  # deliberately much larger
    transcript = Transcript(
        segments=[Segment(text=f"segment {i}", start=i, end=i + 1) for i in range(120)]
    )

    result = polish_transcript(transcript, settings)

    # The endpoint's own budget wins over the global one, so the transcript is
    # split into several small requests instead of one oversized request.
    assert result["detail"]["chunk_chars"] == 3_000
    assert len(seen) > 1
    assert max(seen) <= 3_000 // 80 + 1


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
