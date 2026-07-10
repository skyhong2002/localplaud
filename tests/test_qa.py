"""Tests for single-file Ask: file-scoped retrieval, answer shape, and the
POST /file/{id}/ask web fragment with playable timestamp citations."""

from __future__ import annotations

import numpy as np


def _fresh_db(monkeypatch, tmp_path, name="qa.db"):
    """Point the app at an isolated SQLite DB and reset the engine cache."""
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.session import init_db

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/name}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()


class _FakeEmbedder:
    """Returns a fixed 2-d query vector aligned with the [1, 0] axis."""

    name = "fake"
    dim = 2

    def available(self):
        return True

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


class _FakeLlm:
    name = "fake"

    def available(self):
        return True

    def complete(self, prompt, system=None, temperature=0.3, max_tokens=2048):
        return "Grounded answer."


def _seed_two_files():
    from localplaud.db.models import Chunk, PlaudFile
    from localplaud.db.session import session_scope

    hit = np.array([1.0, 0.0], dtype=np.float32)  # aligns with the query
    miss = np.array([0.0, 1.0], dtype=np.float32)  # orthogonal
    with session_scope() as s:
        s.add(PlaudFile(id="r1", filename="Recording One"))
        s.add(PlaudFile(id="r2", filename="Recording Two"))
        s.add(Chunk(file_id="r1", idx=0, text="r1 relevant", start=12.0, end=15.0,
                    speaker="SPEAKER_00", embedding=hit.tobytes(), dim=2))
        s.add(Chunk(file_id="r1", idx=1, text="r1 offtopic", start=40.0, end=42.0,
                    embedding=miss.tobytes(), dim=2))
        s.add(Chunk(file_id="r2", idx=0, text="r2 relevant", start=3.0, end=6.0,
                    embedding=hit.tobytes(), dim=2))


def test_retrieve_scopes_to_file(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _seed_two_files()
    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda cfg: _FakeEmbedder())
    from localplaud.worker.qa import retrieve

    # Unscoped: both files' relevant chunks surface.
    all_hits = retrieve("q", top_k=6)
    assert {h["file_id"] for h in all_hits} == {"r1", "r2"}

    # Scoped: only the requested recording's chunks are returned.
    scoped = retrieve("q", top_k=6, file_id="r1")
    assert scoped
    assert all(h["file_id"] == "r1" for h in scoped)
    assert scoped[0]["text"] == "r1 relevant"


def test_answer_source_shape_and_scope(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _seed_two_files()
    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda cfg: _FakeEmbedder())
    monkeypatch.setattr("localplaud.worker.qa.build_llm", lambda cfg: _FakeLlm())
    from localplaud.worker.qa import answer

    res = answer("q", file_id="r1")
    assert res["answer"] == "Grounded answer."
    assert res["sources"]
    top = res["sources"][0]
    for key in ("start", "end", "file_id", "filename", "speaker", "score", "text"):
        assert key in top
    assert all(s["file_id"] == "r1" for s in res["sources"])


def test_answer_no_chunks_degrades(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="empty", filename="No Index"))
    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda cfg: _FakeEmbedder())
    from localplaud.worker.qa import answer

    res = answer("q", file_id="empty")
    assert res["sources"] == []
    assert "isn't indexed" in res["answer"]


# --------------------------------------------------------------------------- #
# web fragment
# --------------------------------------------------------------------------- #


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    _fresh_db(monkeypatch, tmp_path, name="ui.db")
    from localplaud.api.app import app

    return TestClient(app)


def _seed_file():
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r1", filename="Weekly Sync", status=FileStatus.done))


def test_file_ask_renders_playable_citations(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed_file()

    def fake_answer(q, top_k=6, settings=None, file_id=None):
        assert file_id == "r1"
        return {
            "answer": "We shipped the beta.",
            "sources": [
                {"score": 0.9, "text": "we agreed to ship the beta", "start": 42.0,
                 "end": 45.0, "speaker": "SPEAKER_00", "file_id": "r1",
                 "filename": "Weekly Sync"}
            ],
        }

    monkeypatch.setattr("localplaud.worker.qa.answer", fake_answer)
    r = c.post("/file/r1/ask", data={"q": "what was decided?"})
    assert r.status_code == 200
    assert "We shipped the beta." in r.text
    assert 'data-seek="42.0"' in r.text
    assert "0:42" in r.text  # mm:ss stamp


def test_file_ask_unknown_file_404(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed_file()
    assert c.post("/file/missing/ask", data={"q": "hi"}).status_code == 404


def test_file_ask_no_chunks_degrades(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed_file()
    # Real qa path: fake embedder, no chunks seeded -> friendly degraded message.
    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda cfg: _FakeEmbedder())
    r = c.post("/file/r1/ask", data={"q": "anything?"})
    assert r.status_code == 200
    assert "indexed yet" in r.text  # apostrophe is HTML-escaped in the fragment
    assert "data-seek" not in r.text


def test_file_ask_provider_unavailable_degrades(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed_file()

    def boom(*a, **k):
        raise RuntimeError("no embeddings")

    monkeypatch.setattr("localplaud.worker.qa.answer", boom)
    r = c.post("/file/r1/ask", data={"q": "anything?"})
    assert r.status_code == 200
    assert "unavailable" in r.text.lower()


def test_detail_page_has_ask_tab_and_deeplink(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed_file()
    r = c.get("/file/r1")
    assert r.status_code == 200
    # Ask tab + panel wired to the single-file endpoint.
    assert 'data-panel="ask"' in r.text
    assert 'hx-post="/file/r1/ask"' in r.text
    assert 'id="file-answer"' in r.text
    # Suggested, grounded, non-mutating chips.
    assert "What was decided?" in r.text
    # Delegated seek handler + ?t= deep-link support.
    assert "data-seek" in r.text
    assert "URLSearchParams" in r.text
