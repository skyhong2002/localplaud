"""Local embeddings via sentence-transformers (CPU-friendly, no network
after the model is downloaded)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .base import EmbeddingUnavailable

if TYPE_CHECKING:
    from ..config import LocalEmbeddingsConfig

log = logging.getLogger(__name__)


class LocalEmbedder:
    """Embeds with a sentence-transformers model, loaded lazily on first use
    and cached for the lifetime of the instance."""

    name = "local"

    def __init__(self, cfg: LocalEmbeddingsConfig) -> None:
        self.cfg = cfg
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise EmbeddingUnavailable(
                    "local embeddings: 'sentence-transformers' is not installed"
                ) from exc
            log.info("Loading sentence-transformers model %s", self.cfg.model)
            self._model = SentenceTransformer(self.cfg.model)
        return self._model

    @property
    def dim(self) -> int:
        """Vector dimensionality (loads the model on first access)."""
        return self._load().get_sentence_embedding_dimension()

    def available(self) -> bool:
        """True if sentence-transformers is importable."""
        try:
            import sentence_transformers  # noqa: F401
        except ImportError:
            return False
        return True

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        return model.encode(texts, normalize_embeddings=True).tolist()
