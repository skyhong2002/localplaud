from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from localplaud.config import OpenAILlmConfig
from localplaud.llm.base import LLMError
from localplaud.llm.openai_llm import OpenAILLM
from localplaud.providers.failures import RetryableProviderError, allows_provider_fallback


def test_openai_compatible_blank_completion_is_rejected(monkeypatch):
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=4),
        choices=[SimpleNamespace(message=SimpleNamespace(content="   "))],
    )
    completions = SimpleNamespace(create=lambda **_kwargs: response)
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=lambda **_kwargs: fake_client),
    )

    provider = OpenAILLM(OpenAILlmConfig(api_key="test", base_url="https://example.test/v1"))
    with pytest.raises(LLMError, match="empty completion"):
        provider.complete("hello")
    assert provider.usage["input_tokens"] == 2
    assert provider.usage["output_tokens"] == 4


def test_openai_compatible_timeout_is_explicitly_retryable(monkeypatch):
    import httpx

    def timeout(**_kwargs):
        raise httpx.ReadTimeout("timed out")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=timeout))
    )
    monkeypatch.setitem(
        sys.modules, "openai", SimpleNamespace(OpenAI=lambda **_kwargs: fake_client)
    )
    provider = OpenAILLM(OpenAILlmConfig(api_key="test"))
    with pytest.raises(RetryableProviderError) as raised:
        provider.complete("hello")
    assert allows_provider_fallback(raised.value) is True
