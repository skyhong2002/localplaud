"""Ollama providers check the configured model and use the modern embed API."""

from __future__ import annotations

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
