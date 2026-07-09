"""Tests for the worker's chunking and Q&A retrieval — no heavy deps needed."""

from __future__ import annotations

import numpy as np

from localplaud.asr.base import Segment, Transcript
from localplaud.worker.index import build_chunks


def _transcript(n: int, chars_each: int = 300) -> Transcript:
    segs = [
        Segment(text="word " * (chars_each // 5), start=float(i), end=float(i + 1), speaker=f"SPEAKER_0{i % 2}")
        for i in range(n)
    ]
    return Transcript(segments=segs)


def test_build_chunks_groups_to_target_size():
    t = _transcript(6, chars_each=300)
    chunks = build_chunks(t, target_chars=700)
    # 6 * ~300 chars = ~1800 -> grouped into a few chunks, each carrying a time range.
    assert 1 < len(chunks) < 6
    for c in chunks:
        assert c["text"]
        assert c["start"] is not None and c["end"] is not None
        assert c["end"] >= c["start"]


def test_build_chunks_skips_empty_segments():
    t = Transcript(segments=[Segment(text="  ", start=0.0, end=1.0), Segment(text="hi", start=1.0, end=2.0)])
    chunks = build_chunks(t)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "hi"


def test_retrieve_ranks_by_cosine(monkeypatch, tmp_path):
    """A tiny fake embedder + seeded chunks: retrieval returns the closest one."""
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.models import Chunk, PlaudFile
    from localplaud.db.session import init_db, session_scope

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'qa.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()

    # Two orthogonal unit vectors; the query aligns with the second.
    a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    b = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    with session_scope() as s:
        s.add(PlaudFile(id="f1", filename="rec"))
        s.add(Chunk(file_id="f1", idx=0, text="about apples", embedding=a.tobytes(), dim=3))
        s.add(Chunk(file_id="f1", idx=1, text="about oranges", embedding=b.tobytes(), dim=3))

    class FakeEmbedder:
        name = "fake"
        dim = 3

        def available(self):
            return True

        def embed(self, texts):
            return [[0.0, 1.0, 0.0] for _ in texts]  # aligns with "oranges"

    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda cfg: FakeEmbedder())
    from localplaud.worker.qa import retrieve

    hits = retrieve("citrus?", top_k=2)
    assert hits[0]["text"] == "about oranges"
    assert hits[0]["score"] > hits[1]["score"]
