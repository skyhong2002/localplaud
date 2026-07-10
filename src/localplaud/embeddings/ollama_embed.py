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
    name = "ollama"

    def __init__(self, cfg: OllamaEmbeddingsConfig):
        self.cfg = cfg
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed(["dimension probe"])[0])
        return self._dim

    def available(self) -> bool:
        try:
            import httpx

            return httpx.get(f"{self.cfg.host}/api/tags", timeout=5).status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def embed(self, texts: list[str]) -> list[list[float]]:
        import httpx

        vectors: list[list[float]] = []
        try:
            with httpx.Client(timeout=300) as client:
                for text in texts:
                    resp = client.post(
                        f"{self.cfg.host}/api/embeddings",
                        json={"model": self.cfg.model, "prompt": text},
                    )
                    resp.raise_for_status()
                    vectors.append(resp.json()["embedding"])
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(f"Ollama embeddings request failed: {exc}") from exc
        except (KeyError, ValueError) as exc:
            raise EmbeddingError(f"unexpected Ollama embeddings response: {exc}") from exc
        return vectors
