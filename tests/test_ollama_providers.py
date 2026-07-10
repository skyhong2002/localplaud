"""Ollama providers check the configured model and use the modern embed API."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

HOST = "http://ollama.test"


@respx.mock
def test_model_health_requires_configured_model():
    from localplaud.ollama import model_health

    respx.get(f"{HOST}/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "qwen3.5:9b"}]})
    )
    ok, detail = model_health(HOST, "bge-m3")
    assert ok is False
    assert "ollama pull bge-m3" in detail


@respx.mock
def test_model_health_accepts_implicit_latest_tag():
    from localplaud.ollama import model_health

    respx.get(f"{HOST}/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"model": "bge-m3:latest"}]})
    )
    assert model_health(HOST, "bge-m3") == (True, "model bge-m3 is installed")


@respx.mock
def test_ollama_embed_uses_modern_batch_endpoint():
    from localplaud.config import OllamaEmbeddingsConfig
    from localplaud.embeddings.ollama_embed import OllamaEmbedder

    route = respx.post(f"{HOST}/api/embed").mock(
        return_value=httpx.Response(200, json={"embeddings": [[1.0, 0.0], [0.0, 1.0]]})
    )
    embedder = OllamaEmbedder(OllamaEmbeddingsConfig(host=HOST, model="bge-m3"))
    assert embedder.embed(["one", "two"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert route.calls[0].request.read()
    assert embedder.name == "ollama:bge-m3"


@respx.mock
def test_ollama_embed_falls_back_for_old_daemon():
    from localplaud.config import OllamaEmbeddingsConfig
    from localplaud.embeddings.ollama_embed import OllamaEmbedder

    respx.post(f"{HOST}/api/embed").mock(
        return_value=httpx.Response(404, text="404 page not found")
    )
    legacy = respx.post(f"{HOST}/api/embeddings").mock(
        side_effect=[
            httpx.Response(200, json={"embedding": [1.0]}),
            httpx.Response(200, json={"embedding": [2.0]}),
        ]
    )
    embedder = OllamaEmbedder(OllamaEmbeddingsConfig(host=HOST, model="bge-m3"))
    assert embedder.embed(["one", "two"]) == [[1.0], [2.0]]
    assert legacy.call_count == 2


@respx.mock
def test_ollama_embed_missing_model_is_actionable():
    from localplaud.config import OllamaEmbeddingsConfig
    from localplaud.embeddings.base import EmbeddingUnavailable
    from localplaud.embeddings.ollama_embed import OllamaEmbedder

    respx.post(f"{HOST}/api/embed").mock(
        return_value=httpx.Response(
            404, json={"error": 'model "bge-m3" not found, try pulling it first'}
        )
    )
    embedder = OllamaEmbedder(OllamaEmbeddingsConfig(host=HOST, model="bge-m3"))
    with pytest.raises(EmbeddingUnavailable, match="ollama pull bge-m3"):
        embedder.embed(["one"])


@respx.mock
def test_ollama_llm_disables_thinking_and_honors_visible_token_budget():
    from localplaud.config import OllamaConfig
    from localplaud.llm.ollama import OllamaProvider

    route = respx.post(f"{HOST}/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "# Demo\n- Ready"}})
    )
    provider = OllamaProvider(OllamaConfig(host=HOST, model="qwen3.5:9b"))
    assert provider.complete("make an outline", max_tokens=321) == "# Demo\n- Ready"
    payload = json.loads(route.calls[0].request.content)
    assert payload["think"] is False
    assert payload["options"]["num_predict"] == 321


@respx.mock
def test_ollama_llm_rejects_empty_visible_completion():
    from localplaud.config import OllamaConfig
    from localplaud.llm.base import LLMError
    from localplaud.llm.ollama import OllamaProvider

    respx.post(f"{HOST}/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={"message": {"content": "", "thinking": "hidden only"}},
        )
    )
    provider = OllamaProvider(OllamaConfig(host=HOST, model="qwen3.5:9b"))
    with pytest.raises(LLMError, match="empty completion"):
        provider.complete("make an outline")
