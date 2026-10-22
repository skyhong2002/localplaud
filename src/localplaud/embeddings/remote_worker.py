from __future__ import annotations

from ..providers.remote_inference import run_remote_json
from .base import EmbeddingError


class RemoteWorkerEmbedder:
    name = "remote-worker"

    def __init__(self, cfg):
        self.cfg = cfg
        self.name = "remote-worker"
        self.space_id = f"remote-worker:{cfg.base_url.rstrip('/')}:{cfg.model}"

    @property
    def dim(self) -> int:
        return self.cfg.dim

    def available(self) -> bool:
        return bool(self.cfg.base_url and self.cfg.token and self.cfg.model)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.available():
            raise EmbeddingError("remote-worker embeddings are not configured")
        try:
            payload = run_remote_json(self.cfg, "embeddings", {"texts": texts})
            vectors = payload.get("vectors")
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise ValueError("vector count mismatch")
            if any(not isinstance(vector, list) or len(vector) != self.cfg.dim
                   for vector in vectors):
                raise ValueError("vector dimension mismatch")
            return vectors
        except Exception as exc:
            raise EmbeddingError(f"remote-worker embeddings failed: {exc}") from exc
