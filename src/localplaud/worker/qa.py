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


def _load_matrix(
    session, dim: int, file_id: str | None = None
) -> tuple[list[Chunk], np.ndarray]:
    # Only chunks embedded at the query's dimension are comparable; mixing dims
    # (e.g. after switching embeddings.provider) would crash np.stack / the dot
    # product. Filter to the current embedder's dimension. When ``file_id`` is
    # set, scope retrieval to a single recording (single-file Ask).
    stmt = select(Chunk).where(Chunk.embedding.is_not(None), Chunk.dim == dim)
    if file_id is not None:
        stmt = stmt.where(Chunk.file_id == file_id)
    chunks = list(session.scalars(stmt))
    if not chunks:
        return [], np.zeros((0, 0), dtype=np.float32)
    vecs = np.stack([np.frombuffer(c.embedding, dtype=np.float32) for c in chunks])
    return chunks, vecs


def retrieve(
    query: str,
    top_k: int = 6,
    settings: Settings | None = None,
    file_id: str | None = None,
) -> list[dict]:
    """Return the top_k most relevant chunks as dicts with score + source.

    When ``file_id`` is provided, retrieval is scoped to that one recording.
    """
    settings = settings or get_settings()
    embedder = build_embedder(settings.embeddings)
    qv = np.asarray(embedder.embed([query])[0], dtype=np.float32)
    qn = qv / (np.linalg.norm(qv) + 1e-8)

    results: list[dict] = []
    with session_scope() as session:
        chunks, mat = _load_matrix(session, dim=len(qv), file_id=file_id)
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

_QA_SYSTEM_SINGLE = (
    "You answer questions about one of the user's own voice recordings, using "
    "only the provided excerpts from it. Reference the moments you rely on by "
    "their timestamp (e.g. \"around 2:30\"). If the excerpts do not contain the "
    "answer, say so plainly rather than guessing."
)


def _format_context(hits: list[dict]) -> str:
    blocks = []
    for h in hits:
        stamp = f" @ {h['start']:.0f}s" if h.get("start") is not None else ""
        blocks.append(f"[{h['filename']}{stamp}] {h['text']}")
    return "\n\n".join(blocks)


def answer(
    query: str,
    top_k: int = 6,
    settings: Settings | None = None,
    file_id: str | None = None,
    history: list[dict] | None = None,
) -> dict:
    """Retrieve + answer. Returns {answer, sources}.

    When ``file_id`` is provided, both retrieval and the answer are scoped to a
    single recording, and the model is asked to reference moments by timestamp.
    """
    settings = settings or get_settings()
    hits = retrieve(query, top_k=top_k, settings=settings, file_id=file_id)
    if not hits:
        if file_id is not None:
            return {
                "answer": "This recording isn't indexed yet — process it first, "
                "then ask again.",
                "sources": [],
            }
        return {"answer": "No indexed recordings yet — run the pipeline first.", "sources": []}
    llm = build_llm(settings.llm)
    system = _QA_SYSTEM_SINGLE if file_id is not None else _QA_SYSTEM
    prior = ""
    if history:
        bounded = history[-8:]
        turns = "\n".join(
            f"{item.get('role', 'user').title()}: {str(item.get('content', ''))[:2000]}"
            for item in bounded
        )
        prior = f"Conversation so far:\n---\n{turns}\n---\n\n"
    prompt = (
        f"{prior}Current question: {query}\n\nExcerpts:\n---\n{_format_context(hits)}\n---\n\n"
        "Answer the question grounded in the excerpts above."
    )
    text = llm.complete(prompt, system=system, temperature=0.2, max_tokens=800)
    return {"answer": text, "sources": hits}
