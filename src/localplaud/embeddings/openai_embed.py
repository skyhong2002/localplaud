"""OpenAI (and OpenAI-compatible) embedding provider."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import EmbeddingUnavailable

if TYPE_CHECKING:
    from ..config import OpenAIEmbeddingsConfig

log = logging.getLogger(__name__)


class OpenAIEmbedder:
    """Embeddings via the OpenAI SDK. ``base_url`` lets this point at any
    OpenAI-compatible server."""

    def __init__(self, cfg: OpenAIEmbeddingsConfig) -> None:
        self.cfg = cfg
        self.name = "openai"
        endpoint = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        self.space_id = f"openai:{endpoint}:{cfg.model}"

    @property
    def dim(self) -> int:
        """Known dimensionalities for OpenAI embedding models."""
        if "3-small" in self.cfg.model:
            return 1536
        if "3-large" in self.cfg.model:
            return 3072
        return 1536

    def available(self) -> bool:
        """True if an API key is configured."""
        return bool(self.cfg.api_key)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.cfg.api_key:
            raise EmbeddingUnavailable("OpenAI embeddings: no API key configured")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "OpenAI embeddings: the 'openai' package is not installed"
            ) from exc

        if self.cfg.max_input_chars is not None and any(
            len(text) > self.cfg.max_input_chars for text in texts
        ):
            raise EmbeddingUnavailable(
                "OpenAI embeddings input exceeds configured max_input_chars"
            )
        client = OpenAI(api_key=self.cfg.api_key, base_url=self.cfg.base_url or None,
                        default_headers=self.cfg.headers or None,
                        timeout=self.cfg.timeout_seconds)
        resp = client.embeddings.create(model=self.cfg.model, input=texts)
        return [d.embedding for d in resp.data]
