"""Transcript corrections as revisions: raw ASR stays immutable, the latest
revision is the corrected canonical transcript, and edits re-index without
rerunning ASR."""

from __future__ import annotations

import time

from sqlalchemy import select


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'ui.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


SEGMENTS = [
    {"text": "hello team", "start": 1.0, "end": 2.0, "speaker": "SPEAKER_00", "words": []},
    {"text": "let's start", "start": 2.0, "end": 3.0, "speaker": "SPEAKER_01", "words": []},
]


def _seed(with_index: bool = False):
    from localplaud.db.models import (
        Chunk,
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
    )
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r1", filename="Weekly Sync", status=FileStatus.done,
                        duration_ms=600000, start_time_ms=1783582737000))
        s.add(Transcript(file_id="r1", provider="faster-whisper", model="large-v3-turbo",
                         language="en", has_speakers=True, source="local",
                         text="hello team\nlet's start", segments=SEGMENTS))
        if with_index:
            s.add(Chunk(file_id="r1", idx=0, text="hello team let's start",
                        start=1.0, end=3.0))
            s.add(StageRun(file_id="r1", stage=StageName.index,
                           status=StageStatus.completed, attempts=1))


def _mute_reindex(monkeypatch):
    """Replace the background re-index with a recorder so tests stay
    deterministic (no embedding provider in the test env)."""
    import localplaud.worker.reindex as reindex_mod

    calls: list[str] = []
    monkeypatch.setattr(reindex_mod, "reindex_file", lambda file_id, settings=None: calls.append(file_id))
    return calls


def test_segment_edit_creates_revision_and_invalidates_index(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    calls = _mute_reindex(monkeypatch)
    from localplaud.db.models import (
        Chunk,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
        TranscriptRevision,
    )
    from localplaud.db.session import session_scope

    r = c.post("/file/r1/transcript/segments/0", data={"text": "hello, team!"},
               follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/file/r1"

    with session_scope() as s:
        revs = list(s.scalars(select(TranscriptRevision).order_by(TranscriptRevision.revision)))
        raw = s.scalar(select(Transcript).where(Transcript.file_id == "r1"))
        chunks = list(s.scalars(select(Chunk).where(Chunk.file_id == "r1")))
        run = s.scalar(select(StageRun).where(StageRun.file_id == "r1",
                                              StageRun.stage == StageName.index))
        assert len(revs) == 1
        rev = revs[0]
        assert rev.revision == 1
        assert rev.base_transcript_id == raw.id
        assert rev.segments[0]["text"] == "hello, team!"
        assert rev.segments[1]["text"] == "let's start"  # untouched segment cloned
        assert rev.text == "hello, team!\nlet's start"
        assert rev.has_speakers is True
        # the raw ASR row is immutable
        assert raw.segments[0]["text"] == "hello team"
        assert raw.text == "hello team\nlet's start"
        # index invalidated without rerunning ASR
        assert chunks == []
        assert run.status == StageStatus.pending
        assert run.error is None

    deadline = time.monotonic() + 2
    while calls != ["r1"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == ["r1"]  # background re-index was kicked off

    # a second edit stacks revision 2 on top of revision 1
    r = c.post("/file/r1/transcript/segments/1", data={"text": "let us start"},
               follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        revs = list(s.scalars(select(TranscriptRevision).order_by(TranscriptRevision.revision)))
        assert [rev.revision for rev in revs] == [1, 2]
        assert revs[1].segments[0]["text"] == "hello, team!"  # keeps earlier edit
        assert revs[1].segments[1]["text"] == "let us start"


def test_segment_edit_validation(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    # index out of range
    assert c.post("/file/r1/transcript/segments/99", data={"text": "x"},
                  follow_redirects=False).status_code == 400
    # unknown file
    assert c.post("/file/nope/transcript/segments/0", data={"text": "x"},
                  follow_redirects=False).status_code == 404
    # file without any transcript
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="bare", filename="bare"))
    assert c.post("/file/bare/transcript/segments/0", data={"text": "x"},
                  follow_redirects=False).status_code == 400


def test_load_transcript_returns_corrected_canonical(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    from localplaud.config import get_settings
    from localplaud.worker.pipeline import _load_transcript

    loaded = _load_transcript("r1", get_settings())
    assert loaded is not None and loaded[0].segments[0].text == "hello team"

    c.post("/file/r1/transcript/segments/0", data={"text": "hello, team!"},
           follow_redirects=False)
    transcript, source = _load_transcript("r1", get_settings())
    assert source == "local"
    assert transcript.segments[0].text == "hello, team!"
    assert transcript.provider == "faster-whisper"  # provenance from the base row
    assert transcript.model == "large-v3-turbo"
    assert transcript.has_speakers is True


def test_repersist_asr_keeps_user_revisions(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    c.post("/file/r1/transcript/segments/0", data={"text": "hello, team!"},
           follow_redirects=False)

    from localplaud.asr.base import Segment, Transcript
    from localplaud.config import get_settings
    from localplaud.db.models import TranscriptRevision
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _load_transcript, _persist_transcript

    # Re-running ASR replaces the raw local row but must not destroy edits.
    _persist_transcript(
        "r1",
        Transcript(segments=[Segment(text="hello again", start=0.0, end=1.0)],
                   provider="mlx-whisper"),
    )
    with session_scope() as s:
        rev = s.scalar(select(TranscriptRevision))
        assert rev is not None
        assert rev.base_transcript_id is None  # base replaced -> pointer detached
    transcript, source = _load_transcript("r1", get_settings())
    assert source == "local"
    assert transcript.segments[0].text == "hello, team!"
    assert transcript.provider == "local-edit"  # base row gone, provenance labelled


def test_detail_view_toggle_raw_vs_corrected(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)

    # before any edit: raw view, no toggle, editing enabled
    page = c.get("/file/r1")
    assert page.status_code == 200
    assert "raw ASR" in page.text
    assert "?view=raw" not in page.text
    assert 'class="editbtn"' in page.text

    c.post("/file/r1/transcript/segments/0", data={"text": "hello, team!"},
           follow_redirects=False)

    # default view is now the corrected canonical transcript
    page = c.get("/file/r1")
    assert "hello, team!" in page.text
    assert "Corrected (rev 1)" in page.text  # labelled current view
    assert '?view=raw' in page.text  # toggle to the raw artifact
    assert 'class="editbtn"' in page.text

    # explicit raw view shows the untouched ASR output, read-only
    raw = c.get("/file/r1?view=raw")
    assert "hello team" in raw.text
    assert "hello, team!" not in raw.text
    assert "?view=corrected" in raw.text
    assert 'class="editbtn"' not in raw.text  # no edits from the raw view


def test_reindex_file_rebuilds_chunks_from_corrected_transcript(monkeypatch, tmp_path):
    from localplaud.worker.reindex import reindex_file  # real fn, before muting

    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    _mute_reindex(monkeypatch)  # keeps the endpoint's background thread inert
    c.post("/file/r1/transcript/segments/0", data={"text": "hello, team!"},
           follow_redirects=False)

    import localplaud.worker.index as index_mod
    from localplaud.db.models import Chunk, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope

    monkeypatch.setattr(
        index_mod, "embed_chunks",
        lambda chunks, settings: ([b"\x00\x00\x80?" for _ in chunks], "fake-embed", 1),
    )
    assert reindex_file("r1") is True
    with session_scope() as s:
        chunks = list(s.scalars(select(Chunk).where(Chunk.file_id == "r1")))
        run = s.scalar(select(StageRun).where(StageRun.file_id == "r1",
                                              StageRun.stage == StageName.index))
        assert chunks and "hello, team!" in chunks[0].text  # corrected text indexed
        assert all(chunk.embedding_model == "fake-embed" for chunk in chunks)
        assert run.status == StageStatus.completed


def test_reindex_file_failure_is_durable(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()

    import localplaud.worker.index as index_mod
    from localplaud.db.models import StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker.reindex import reindex_file

    def boom(chunks, settings):
        raise RuntimeError("embedding model unavailable")

    monkeypatch.setattr(index_mod, "embed_chunks", boom)
    assert reindex_file("r1") is False
    with session_scope() as s:
        run = s.scalar(select(StageRun).where(StageRun.file_id == "r1",
                                              StageRun.stage == StageName.index))
        assert run.status == StageStatus.failed
        assert "embedding model unavailable" in run.error
