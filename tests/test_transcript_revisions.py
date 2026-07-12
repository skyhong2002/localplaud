"""Transcript corrections as revisions: raw ASR stays immutable, the latest
revision is the corrected canonical transcript, and edits re-index without
rerunning ASR."""

from __future__ import annotations

import time

from sqlalchemy import create_engine, inspect, select, text


def test_local_transcript_uniqueness_migration_preserves_cloud_and_revision(tmp_path):
    from localplaud.db.migrations import migrate_local_transcript_uniqueness

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id VARCHAR(64) NOT NULL,
                source VARCHAR(16) NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE transcript_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                base_transcript_id INTEGER
            )
        """))
        connection.execute(text("""
            INSERT INTO transcripts (id, file_id, source) VALUES
                (1, 'recording', 'local'),
                (2, 'recording', 'local'),
                (3, 'recording', 'cloud'),
                (4, 'recording', 'cloud')
        """))
        connection.execute(
            text("INSERT INTO transcript_revisions (base_transcript_id) VALUES (1), (2)")
        )

    assert migrate_local_transcript_uniqueness(engine) == ["transcripts.local"]
    assert migrate_local_transcript_uniqueness(engine) == []
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, source FROM transcripts ORDER BY id")
        ).all()
        revision_bases = connection.execute(
            text("SELECT base_transcript_id FROM transcript_revisions ORDER BY id")
        ).scalars().all()
    assert rows == [(2, "local"), (3, "cloud"), (4, "cloud")]
    assert revision_bases == [None, 2]
    indexes = {item["name"] for item in inspect(engine).get_indexes("transcripts")}
    assert "uq_transcripts_one_local_per_file" in indexes


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
    monkeypatch.setattr(
        reindex_mod,
        "reindex_file",
        lambda file_id, settings=None, **kwargs: calls.append(file_id),
    )
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

    r = c.post("/file/r1/transcript/segments/0", data={"text": "hello, team!", "base_revision": 0},
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
    r = c.post("/file/r1/transcript/segments/1", data={"text": "let us start", "base_revision": 1},
               follow_redirects=False)
    assert r.status_code == 303
    with session_scope() as s:
        revs = list(s.scalars(select(TranscriptRevision).order_by(TranscriptRevision.revision)))
        assert [rev.revision for rev in revs] == [1, 2]
        assert revs[1].segments[0]["text"] == "hello, team!"  # keeps earlier edit
        assert revs[1].segments[1]["text"] == "let us start"


def test_find_replace_creates_one_bulk_revision(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    calls = _mute_reindex(monkeypatch)
    from localplaud.db.models import Chunk, Transcript, TranscriptRevision
    from localplaud.db.session import session_scope

    response = c.post(
        "/file/r1/transcript/replace",
        data={
            "find": "TEAM",
            "replace": "everyone",
            "base_revision": 0,
            "case_sensitive": "false",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"replacements": 1, "revision": 1}
    with session_scope() as session:
        revision = session.scalar(select(TranscriptRevision))
        raw = session.scalar(select(Transcript).where(Transcript.file_id == "r1"))
        assert revision.text == "hello everyone\nlet's start"
        assert revision.note == 'replaced "TEAM" (1 occurrence(s))'
        assert raw.text == "hello team\nlet's start"
        assert session.query(Chunk).filter_by(file_id="r1").count() == 0
    assert calls == ["r1"]


def test_find_replace_no_match_and_stale_revision_are_safe(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    no_match = c.post(
        "/file/r1/transcript/replace",
        data={"find": "missing", "replace": "x", "base_revision": 0},
    )
    assert no_match.json() == {"replacements": 0, "revision": 0}
    c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "first edit", "base_revision": 0},
        follow_redirects=False,
    )
    stale = c.post(
        "/file/r1/transcript/replace",
        data={"find": "start", "replace": "finish", "base_revision": 0},
    )
    assert stale.status_code == 409


def test_revision_history_preview_and_non_destructive_restore(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    calls = _mute_reindex(monkeypatch)
    from localplaud.db.models import TranscriptRevision
    from localplaud.db.session import session_scope

    c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "first version", "base_revision": 0},
        follow_redirects=False,
    )
    c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "second version", "base_revision": 1},
        follow_redirects=False,
    )
    preview = c.get("/file/r1?view=corrected&revision=1")
    assert preview.status_code == 200
    assert "Revision 1 preview" in preview.text and "first version" in preview.text
    assert "second version" not in preview.text
    assert "Revision history · 2" in preview.text

    restored = c.post(
        "/file/r1/transcript/revisions/1/restore",
        data={"base_revision": 2},
        follow_redirects=False,
    )
    assert restored.status_code == 303
    with session_scope() as session:
        revisions = list(
            session.scalars(
                select(TranscriptRevision).order_by(TranscriptRevision.revision)
            )
        )
        assert [row.revision for row in revisions] == [1, 2, 3]
        assert revisions[-1].text == "first version\nlet's start"
        assert revisions[-1].note == "restored revision 1"
    assert calls.count("r1") == 3


def test_revision_restore_rejects_stale_or_unknown_revision(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "revision one", "base_revision": 0},
        follow_redirects=False,
    )
    assert c.post(
        "/file/r1/transcript/revisions/99/restore",
        data={"base_revision": 1},
    ).status_code == 404
    assert c.post(
        "/file/r1/transcript/revisions/1/restore",
        data={"base_revision": 0},
    ).status_code == 409


def test_derived_artifacts_record_exact_transcript_revision(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "canonical revision", "base_revision": 0},
        follow_redirects=False,
    )
    import localplaud.worker.index as index_mod
    from localplaud.config import get_settings
    from localplaud.db.models import Chunk, Summary
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import (
        _finish_stage,
        _load_transcript,
        _persist_chunks,
        _persist_summary,
        _transcript_lineage,
    )

    monkeypatch.setattr(
        index_mod,
        "embed_chunks",
        lambda chunks, settings: ([b"\x00\x00\x80?" for _ in chunks], "fake", 1),
    )
    settings = get_settings()
    lineage = _transcript_lineage("r1", settings)
    transcript, _source = _load_transcript("r1", settings)
    _persist_summary(
        "r1",
        {"template": "lineage", "content_md": "# From corrected transcript"},
        lineage,
    )
    from localplaud.db.models import StageName

    _finish_stage("r1", StageName.summarize, artifact_source="local", detail={})
    _persist_chunks("r1", transcript, settings, lineage)
    with session_scope() as session:
        summary = session.query(Summary).filter_by(file_id="r1", template="lineage").one()
        chunk = session.query(Chunk).filter_by(file_id="r1").first()
        assert lineage == {
            "input_transcript_id": summary.input_transcript_id,
            "input_transcript_revision": 1,
            "input_transcript_source": "local",
        }
        assert chunk.input_transcript_id == summary.input_transcript_id
        assert chunk.input_transcript_revision == 1
        assert chunk.input_transcript_source == "local"
    page = c.get("/file/r1?view=corrected")
    assert "Generated from transcript rev 1 · local" in page.text


def test_segment_edit_validation(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    # index out of range
    assert c.post("/file/r1/transcript/segments/99", data={"text": "x", "base_revision": 0},
                  follow_redirects=False).status_code == 400
    # unknown file
    assert c.post("/file/nope/transcript/segments/0", data={"text": "x", "base_revision": 0},
                  follow_redirects=False).status_code == 404
    # file without any transcript
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="bare", filename="bare"))
    assert c.post("/file/bare/transcript/segments/0", data={"text": "x", "base_revision": 0},
                  follow_redirects=False).status_code == 400


def test_load_transcript_returns_corrected_canonical(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    from localplaud.config import get_settings
    from localplaud.worker.pipeline import _load_transcript

    loaded = _load_transcript("r1", get_settings())
    assert loaded is not None and loaded[0].segments[0].text == "hello team"

    c.post("/file/r1/transcript/segments/0", data={"text": "hello, team!", "base_revision": 0},
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
    c.post("/file/r1/transcript/segments/0", data={"text": "hello, team!", "base_revision": 0},
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

    c.post("/file/r1/transcript/segments/0", data={"text": "hello, team!", "base_revision": 0},
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
    c.post("/file/r1/transcript/segments/0", data={"text": "hello, team!", "base_revision": 0},
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


def test_force_rebuild_uses_preserved_corrected_canonical_downstream(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONVERT", "false")
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "corrected canonical", "base_revision": 0},
        follow_redirects=False,
    )

    from localplaud.asr.base import Segment, Transcript
    from localplaud.config import get_settings
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import process_file

    audio = tmp_path / "force.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as s:
        s.get(PlaudFile, "r1").audio_path = str(audio)

    seen = {}

    monkeypatch.setattr(
        "localplaud.worker.pipeline.transcribe.run_asr",
        lambda wav, settings: Transcript(
            segments=[Segment(text="new raw ASR", start=0.0, end=1.0, speaker="SPEAKER_00")],
            provider="fake",
            has_speakers=True,
        ),
    )

    def fake_summary(transcript, settings):
        seen["summary"] = transcript.text
        return {
            "title": "T",
            "content_md": "# T",
            "provider": "fake",
            "model": "m",
            "template": settings.pipeline.summary_template,
        }

    def fake_mindmap(transcript, settings, summary_md=None):
        seen["mindmap"] = transcript.text
        return {
            "template": "mind_map",
            "content_md": "# T\n- point",
            "provider": "fake",
            "model": "m",
            "detail": {},
        }

    def fake_embed(chunks, settings):
        seen["index"] = " ".join(chunk["text"] for chunk in chunks)
        return [b"\x00\x00\x80?" for _ in chunks], "fake", 1

    monkeypatch.setattr("localplaud.worker.pipeline.summarize.summarize", fake_summary)
    monkeypatch.setattr("localplaud.worker.pipeline.mindmap.generate_mind_map", fake_mindmap)
    monkeypatch.setattr("localplaud.worker.pipeline.index.embed_chunks", fake_embed)

    process_file("r1", settings=get_settings(), force=True)
    assert seen == {
        "summary": "corrected canonical\nlet's start",
        "mindmap": "corrected canonical\nlet's start",
        "index": "corrected canonical let's start",
    }


def test_cloud_derived_revision_never_satisfies_independent_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__ARTIFACT_MODE", "migration")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__PREFER_CLOUD_ARTIFACTS", "true")
    c = _client(monkeypatch, tmp_path)
    _mute_reindex(monkeypatch)

    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile, Transcript, TranscriptRevision
    from localplaud.db.session import session_scope
    from localplaud.exporter import render_markdown
    from localplaud.worker.pipeline import _load_transcript

    with session_scope() as s:
        s.add(PlaudFile(id="cloud", filename="Cloud", status=FileStatus.done))
        s.add(
            Transcript(
                file_id="cloud",
                provider="plaud",
                source="cloud",
                text="paid cloud text",
                segments=[{"text": "paid cloud text", "start": 0.0, "end": 1.0}],
            )
        )

    response = c.post(
        "/file/cloud/transcript/segments/0",
        data={"text": "edited cloud text", "base_revision": 0},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with session_scope() as s:
        revision = s.scalar(select(TranscriptRevision).where(TranscriptRevision.file_id == "cloud"))
        assert revision.source == "cloud"

    transcript, source = _load_transcript("cloud", get_settings())
    assert source == "cloud"
    assert transcript.text == "edited cloud text"

    monkeypatch.setenv("LOCALPLAUD_PIPELINE__ARTIFACT_MODE", "independent")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__PREFER_CLOUD_ARTIFACTS", "false")
    independent = get_settings(reload=True)
    assert _load_transcript("cloud", independent) is None
    assert "edited cloud text" not in render_markdown("cloud")
    page = c.get("/file/cloud")
    assert "edited cloud text" not in page.text


def test_stale_edit_is_rejected_without_losing_first_revision(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)

    first = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "first edit", "base_revision": 0},
        follow_redirects=False,
    )
    stale = c.post(
        "/file/r1/transcript/segments/1",
        data={"text": "stale edit", "base_revision": 0},
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert stale.status_code == 409

    from localplaud.db.models import TranscriptRevision
    from localplaud.db.session import session_scope

    with session_scope() as s:
        revisions = list(s.scalars(select(TranscriptRevision)))
        assert len(revisions) == 1
        assert revisions[0].segments[0]["text"] == "first edit"
        assert revisions[0].segments[1]["text"] == "let's start"


def test_edit_hides_stale_notes_and_marks_regeneration_pending(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)

    from localplaud.db.models import PlaudFile, StageName, StageStatus, Summary
    from localplaud.db.session import session_scope
    from localplaud.exporter import render_markdown
    from localplaud.worker.pipeline import _has_summary

    with session_scope() as s:
        s.add(
            Summary(
                file_id="r1",
                template="default",
                source="local",
                content_md="STALE NOTE",
            )
        )
        s.add(
            Summary(
                file_id="r1",
                template="mind_map",
                source="local",
                content_md="# STALE MAP\n- old",
            )
        )

    response = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "fresh correction", "base_revision": 0},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "STALE NOTE" not in c.get("/file/r1").text
    assert "STALE NOTE" not in render_markdown("r1")
    assert "STALE MAP" not in render_markdown("r1")
    assert _has_summary("r1", "default") is False
    assert _has_summary("r1", "mind_map") is False

    with session_scope() as s:
        runs = {run.stage: run for run in s.get(PlaudFile, "r1").stage_runs}
        for stage in (StageName.summarize, StageName.mind_map, StageName.index):
            assert runs[stage].status == StageStatus.pending
            assert runs[stage].detail["stale"] is True


def test_superseded_background_reindex_is_fenced(monkeypatch, tmp_path):
    from localplaud.worker.reindex import reindex_file

    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    _mute_reindex(monkeypatch)
    c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "newest", "base_revision": 0},
        follow_redirects=False,
    )

    import localplaud.worker.index as index_mod
    called = False

    def should_not_embed(chunks, settings):
        nonlocal called
        called = True
        raise AssertionError("superseded job must not embed")

    monkeypatch.setattr(index_mod, "embed_chunks", should_not_embed)
    assert reindex_file("r1", expected_revision=0) is False
    assert called is False
