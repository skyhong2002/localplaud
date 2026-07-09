"""Q&A and semantic search over the transcript knowledge base.

localplaud's answer to "Ask Plaud": embed the question, retrieve the closest
chunks by cosine similarity, and let the LLM answer grounded in them, citing
the source recordings.
"""

from __future__ import annotations

import logging

import numpy as np
from sqlalchemy import select

from ..config import Settings, get_settings
from ..db.models import Chunk, PlaudFile
from ..db.session import session_scope
from ..embeddings.base import build_embedder
from ..llm.base import build_llm

log = logging.getLogger(__name__)


def _load_matrix(session, dim: int) -> tuple[list[Chunk], np.ndarray]:
    # Only chunks embedded at the query's dimension are comparable; mixing dims
    # (e.g. after switching embeddings.provider) would crash np.stack / the dot
    # product. Filter to the current embedder's dimension.
    chunks = list(
        session.scalars(
            select(Chunk).where(Chunk.embedding.is_not(None), Chunk.dim == dim)
        )
    )
    if not chunks:
        return [], np.zeros((0, 0), dtype=np.float32)
    vecs = np.stack([np.frombuffer(c.embedding, dtype=np.float32) for c in chunks])
    return chunks, vecs


def retrieve(query: str, top_k: int = 6, settings: Settings | None = None) -> list[dict]:
    """Return the top_k most relevant chunks as dicts with score + source."""
    settings = settings or get_settings()
    embedder = build_embedder(settings.embeddings)
    qv = np.asarray(embedder.embed([query])[0], dtype=np.float32)
    qn = qv / (np.linalg.norm(qv) + 1e-8)

    results: list[dict] = []
    with session_scope() as session:
        chunks, mat = _load_matrix(session, dim=len(qv))
        if not chunks:
            return []
        norms = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-8)
        scores = norms @ qn
        top = np.argsort(-scores)[:top_k]
        for i in top:
            c = chunks[int(i)]
            f = session.get(PlaudFile, c.file_id)
            results.append(
                {
                    "score": float(scores[int(i)]),
                    "text": c.text,
                    "start": c.start,
                    "end": c.end,
                    "speaker": c.speaker,
                    "file_id": c.file_id,
                    "filename": f.filename if f else c.file_id,
                }
            )
    return results


_QA_SYSTEM = (
    "You answer questions using only the provided excerpts from the user's own "
    "voice recordings. Cite the recording titles you used. If the excerpts do "
    "not contain the answer, say so plainly."
)


def _format_context(hits: list[dict]) -> str:
    blocks = []
    for h in hits:
        stamp = f" @ {h['start']:.0f}s" if h.get("start") is not None else ""
        blocks.append(f"[{h['filename']}{stamp}] {h['text']}")
    return "\n\n".join(blocks)


def answer(query: str, top_k: int = 6, settings: Settings | None = None) -> dict:
    """Retrieve + answer. Returns {answer, sources}."""
    settings = settings or get_settings()
    hits = retrieve(query, top_k=top_k, settings=settings)
    if not hits:
        return {"answer": "No indexed recordings yet — run the pipeline first.", "sources": []}
    llm = build_llm(settings.llm)
    prompt = (
        f"Question: {query}\n\nExcerpts:\n---\n{_format_context(hits)}\n---\n\n"
        "Answer the question grounded in the excerpts above."
    )
    text = llm.complete(prompt, system=_QA_SYSTEM, temperature=0.2, max_tokens=800)
    return {"answer": text, "sources": hits}
