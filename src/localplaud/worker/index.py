"""Indexing stage — chunk a transcript and store embeddings for Q&A / search.

Chunks are built from transcript segments, grouped up to a target character
length so each chunk is a coherent, retrievable passage that keeps its time
range and (if known) speaker. Embeddings are stored as float32 blobs on the
``Chunk`` rows; retrieval is brute-force cosine (see ``qa``), which is plenty
fast at personal scale.
"""

from __future__ import annotations

import logging

import numpy as np

from ..asr.base import Transcript as AsrTranscript
from ..config import Settings
from ..embeddings.base import build_embedder

log = logging.getLogger(__name__)


def build_chunks(transcript: AsrTranscript, target_chars: int = 700) -> list[dict]:
    """Group segments into chunks of ~target_chars. Returns list of dicts with
    text/start/end/speaker."""
    chunks: list[dict] = []
    cur: list[str] = []
    cur_start: float | None = None
    cur_end: float | None = None
    cur_speaker: str | None = None

    def flush():
        nonlocal cur, cur_start, cur_end, cur_speaker
        if cur:
            chunks.append(
                {
                    "text": " ".join(cur).strip(),
                    "start": cur_start,
                    "end": cur_end,
                    "speaker": cur_speaker,
                }
            )
        cur, cur_start, cur_end, cur_speaker = [], None, None, None

    for seg in transcript.segments:
        t = seg.text.strip()
        if not t:
            continue
        if cur_start is None:
            cur_start = seg.start
            cur_speaker = seg.speaker
        cur.append(t)
        cur_end = seg.end
        if sum(len(x) for x in cur) >= target_chars:
            flush()
    flush()
    return chunks


def embed_chunks(chunks: list[dict], settings: Settings) -> tuple[list[bytes], str, int]:
    """Embed chunk texts. Returns (blobs, model_name, dim)."""
    embedder = build_embedder(settings.embeddings)
    vectors = embedder.embed([c["text"] for c in chunks])
    blobs = [np.asarray(v, dtype=np.float32).tobytes() for v in vectors]
    dim = len(vectors[0]) if vectors else 0
    return blobs, embedder.name, dim
