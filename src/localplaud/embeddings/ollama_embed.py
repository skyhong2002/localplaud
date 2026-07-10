"""Local embeddings via Ollama — a torch-free alternative to sentence-transformers.

Uses Ollama's embeddings endpoint, so any embedding model you've pulled (e.g.
``bge-m3``, ``nomic-embed-text``) works with just a running Ollama daemon.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import EmbeddingError, EmbeddingUnavailable

if TYPE_CHECKING:
    from ..config import OllamaEmbeddingsConfig

log = logging.getLogger(__name__)


class OllamaEmbedder:
    def __init__(self, cfg: OllamaEmbeddingsConfig):
        self.cfg = cfg
        self.name = f"ollama:{cfg.model}"
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed(["dimension probe"])[0])
        return self._dim

    def available(self) -> bool:
        return self.health()[0]

    def health(self) -> tuple[bool, str]:
        from ..ollama import model_health

        return model_health(self.cfg.host, self.cfg.model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        if not texts:
            return []
        host = self.cfg.host.rstrip("/")
        try:
            with httpx.Client(timeout=300) as client:
                response = client.post(
                    f"{host}/api/embed",
                    json={"model": self.cfg.model, "input": texts},
                )
                if response.status_code == 404:
                    from ..ollama import response_error

                    error = response_error(response)
                    if "model" in error.lower() and "not found" in error.lower():
                        raise EmbeddingUnavailable(
                            f"Ollama model {self.cfg.model!r} is not installed; "
                            f"run `ollama pull {self.cfg.model}`"
                        )
                    # Ollama before /api/embed accepted one prompt at a time.
                    vectors = []
                    for text in texts:
                        legacy = client.post(
                            f"{host}/api/embeddings",
                            json={"model": self.cfg.model, "prompt": text},
                        )
                        if legacy.status_code == 404:
                            legacy_error = response_error(legacy)
                            if "model" in legacy_error.lower():
                                raise EmbeddingUnavailable(
                                    f"Ollama model {self.cfg.model!r} is not installed; "
                                    f"run `ollama pull {self.cfg.model}`"
                                )
                        legacy.raise_for_status()
                        vectors.append(legacy.json()["embedding"])
                    return vectors
                response.raise_for_status()
                vectors = response.json()["embeddings"]
                if len(vectors) != len(texts):
                    raise EmbeddingError(
                        f"Ollama returned {len(vectors)} embeddings for {len(texts)} inputs"
                    )
                return vectors
        except EmbeddingError:
            raise
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(f"Ollama embeddings request failed: {exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"unexpected Ollama embeddings response: {exc}") from exc
