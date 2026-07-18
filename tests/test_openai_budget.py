"""Hard zero-cost gate for calls to the real OpenAI API."""

from __future__ import annotations

import sys
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

import localplaud.openai_budget as budget_module
from localplaud.config import OpenAIEmbeddingsConfig, OpenAILlmConfig, Settings
from localplaud.embeddings.base import EmbeddingError
from localplaud.embeddings.openai_embed import OpenAIEmbedder
from localplaud.llm.base import LLMError
from localplaud.llm.openai_llm import OpenAILLM
from localplaud.openai_budget import OpenAIBudgetBlocked, assert_openai_free_pool


@pytest.fixture(autouse=True)
def _clear_usage_cache():
    budget_module._usage_cache.clear()
    yield
    budget_module._usage_cache.clear()


def _settings(**overrides) -> Settings:
    config = {
        "enabled": True,
        "admin_key": "env:OPENAI_ADMIN_KEY",
        "safety_margin_fraction": 0,
    }
    config.update(overrides)
    return Settings(openai_budget=config)


def _usage_payload(*results: dict, has_more: bool = False, next_page: str | None = None):
    return {
        "data": [{"results": list(results)}],
        "has_more": has_more,
        "next_page": next_page,
    }


def _mock_all_usage(mock: respx.MockRouter, *completion_results: dict) -> None:
    mock.get("https://api.openai.com/v1/organization/usage/completions").mock(
        return_value=httpx.Response(200, json=_usage_payload(*completion_results))
    )
    for endpoint in ("embeddings", "audio_transcriptions"):
        mock.get(f"https://api.openai.com/v1/organization/usage/{endpoint}").mock(
            return_value=httpx.Response(200, json=_usage_payload())
        )


def _fake_chat_sdk(monkeypatch):
    requests: list[dict] = []

    class Completions:
        def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="allowed"))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda **_kwargs: client))
    return requests


def test_disabled_gate_makes_no_usage_call_and_adapter_proceeds(monkeypatch):
    settings = Settings(openai_budget={"enabled": False})
    monkeypatch.setattr("localplaud.llm.openai_llm.get_settings", lambda: settings)
    requests = _fake_chat_sdk(monkeypatch)
    provider = OpenAILLM(OpenAILlmConfig(api_key="test", model="gpt-4.1"))

    with respx.mock:
        assert provider.complete("hello", max_tokens=10) == "allowed"
        assert respx.calls.call_count == 0
    assert len(requests) == 1


def test_under_limit_is_allowed_and_totals_are_cached(monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "admin-test")
    settings = _settings()

    with respx.mock as mock:
        _mock_all_usage(
            mock,
            {"model": "gpt-4.1", "input_tokens": 100, "output_tokens": 20},
            {
                "model": "GPT-4O-MINI",
                "input_tokens": 500,
                "output_tokens": 50,
                "input_cached_tokens": 999_999,
            },
        )
        assert_openai_free_pool(settings, model="gpt-4.1", projected_tokens=100)
        assert_openai_free_pool(settings, model="gpt-4.1", projected_tokens=100)
        assert mock.calls.call_count == 3


def test_high_pool_block_has_zero_chat_egress_while_mini_pool_is_independent(monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "admin-test")
    settings = _settings()
    monkeypatch.setattr("localplaud.llm.openai_llm.get_settings", lambda: settings)
    requests = _fake_chat_sdk(monkeypatch)

    with respx.mock as mock:
        _mock_all_usage(
            mock,
            {"model": "gpt-4.1", "input_tokens": 249_000, "output_tokens": 1_000},
        )
        high = OpenAILLM(OpenAILlmConfig(api_key="test", model="gpt-4.1"))
        with pytest.raises(LLMError, match="high pool 250,000/250,000"):
            high.complete("blocked", max_tokens=1)
        assert requests == []

        mini = OpenAILLM(OpenAILlmConfig(api_key="test", model="GPT-4O-MINI"))
        assert mini.complete("allowed", max_tokens=1) == "allowed"
        assert len(requests) == 1
        assert mock.calls.call_count == 3


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(401, text="invalid admin key"), httpx.ConnectError("offline")],
)
def test_usage_api_failure_blocks_closed(monkeypatch, failure):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "admin-test")
    settings = _settings()

    with respx.mock as mock:
        mock.get("https://api.openai.com/v1/organization/usage/completions").mock(
            return_value=failure if isinstance(failure, httpx.Response) else None,
            side_effect=failure if isinstance(failure, Exception) else None,
        )
        with pytest.raises(OpenAIBudgetBlocked, match="could not verify the free pool"):
            assert_openai_free_pool(settings, model="gpt-4.1", projected_tokens=1)


def test_missing_admin_key_blocks_with_setup_message(monkeypatch):
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)

    with pytest.raises(OpenAIBudgetBlocked, match="OPENAI_ADMIN_KEY is not set"):
        assert_openai_free_pool(_settings(), model="gpt-4.1", projected_tokens=1)


def test_compatible_relay_bypasses_gate(monkeypatch):
    settings = _settings()
    monkeypatch.setattr("localplaud.llm.openai_llm.get_settings", lambda: settings)
    monkeypatch.setattr(
        "localplaud.llm.openai_llm.assert_openai_free_pool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("gate consulted")),
    )
    requests = _fake_chat_sdk(monkeypatch)
    provider = OpenAILLM(
        OpenAILlmConfig(
            api_key="relay-key",
            base_url="https://relay.example/v1",
            model="gpt-4.1",
        )
    )

    assert provider.complete("hello", max_tokens=1) == "allowed"
    assert len(requests) == 1


def test_timezone_start_time_is_midnight_in_asia_taipei(monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "admin-test")
    real_datetime = datetime

    class FrozenDateTime:
        @classmethod
        def now(cls, timezone):
            return real_datetime(2026, 7, 18, 15, 30, tzinfo=timezone)

    monkeypatch.setattr(budget_module, "datetime", FrozenDateTime)
    expected = int(real_datetime.fromisoformat("2026-07-18T00:00:00+08:00").timestamp())

    with respx.mock as mock:
        _mock_all_usage(mock)
        assert_openai_free_pool(_settings(), model="gpt-4.1", projected_tokens=1)
        for call in mock.calls:
            assert call.request.url.params["start_time"] == str(expected)


def test_embeddings_block_before_sdk_client_or_embedding_egress(monkeypatch):
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "admin-test")
    settings = _settings()
    monkeypatch.setattr("localplaud.embeddings.openai_embed.get_settings", lambda: settings)
    sdk_clients: list[dict] = []
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=lambda **kwargs: sdk_clients.append(kwargs)),
    )

    with respx.mock as mock:
        _mock_all_usage(
            mock,
            {"model": "gpt-4.1", "input_tokens": 250_000, "output_tokens": 0},
        )
        embedder = OpenAIEmbedder(
            OpenAIEmbeddingsConfig(api_key="test", model="text-embedding-3-small")
        )
        with pytest.raises(EmbeddingError, match="high pool 250,000/250,000"):
            embedder.embed(["must not leave the process"])
        assert sdk_clients == []
