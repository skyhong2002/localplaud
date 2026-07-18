"""Transcript corrections as revisions: raw ASR stays immutable, the latest
revision is the corrected canonical transcript, and edits re-index without
rerunning ASR."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
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


def test_revision_provenance_migration_is_idempotent(tmp_path):
    from localplaud.db.migrations import migrate_transcript_revision_provenance

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-revisions.db'}")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE transcript_revisions (
                id INTEGER PRIMARY KEY,
                file_id VARCHAR(64),
                revision INTEGER,
                note VARCHAR(256)
            )
        """))
        connection.execute(text("""
            INSERT INTO transcript_revisions (id, file_id, revision, note)
            VALUES (1, 'recording', 1, 'manual correction')
        """))
    assert set(migrate_transcript_revision_provenance(engine)) == {
        "transcript_revisions.kind",
        "transcript_revisions.provider",
        "transcript_revisions.model",
        "transcript_revisions.prompt_version",
        "transcript_revisions.resolved_profile_snapshot",
    }
    assert migrate_transcript_revision_provenance(engine) == []
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT kind, provider FROM transcript_revisions WHERE id=1")
        ).one()
    assert row.kind == "user_edit" and row.provider is None


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
    {
        "text": "hello team",
        "start": 1.0,
        "end": 2.0,
        "speaker": "SPEAKER_00",
        "words": [
            {
                "text": "hello",
                "start": 1.0,
                "end": 1.4,
                "speaker": "SPEAKER_00",
                "confidence": 0.91,
            },
            {
                "text": "team",
                "start": 1.5,
                "end": 2.0,
                "speaker": "SPEAKER_00",
                "confidence": 0.88,
            },
        ],
    },
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
    assert r.headers["location"] == (
        "/file/r1?return_to=%2F&tab=transcript&view=corrected"
    )

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
        assert rev.segments[0]["words"] == []
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
        assert run.detail["reindex_only"] is True
        assert run.detail["reason"] == "canonical transcript changed"

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


def test_transcript_edit_keeps_durable_reindex_queue_when_thread_start_fails(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    from localplaud.db.models import Chunk, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope

    class BrokenThread:
        def __init__(self, **_kwargs):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr("threading.Thread", BrokenThread)
    response = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "durably corrected", "base_revision": 0},
        headers={"accept": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["revision"] == 1
    with session_scope() as session:
        assert session.query(Chunk).filter_by(file_id="r1").count() == 0
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.index
            )
        )
        assert run.status == StageStatus.pending
        assert run.detail["reindex_only"] is True


def test_transcript_and_speaker_edits_reject_active_ask_lease(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    from localplaud.db.models import AskThread, Speaker, TranscriptRevision
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            AskThread(
                id="active-recording-ask",
                file_id="r1",
                title="Active recording Ask",
                request_token="active-request",
                request_lease_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        )

    edit = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "must not commit", "base_revision": 0},
        headers={"accept": "application/json"},
    )
    rename = c.post(
        "/file/r1/speakers",
        data={"key": "SPEAKER_00", "name": "Must not commit"},
        follow_redirects=False,
    )
    assert edit.status_code == rename.status_code == 409
    assert "used by Ask" in edit.json()["detail"]
    assert "used by Ask" in rename.json()["detail"]
    with session_scope() as session:
        assert session.scalar(select(TranscriptRevision.id)) is None
        assert session.scalar(select(Speaker.id)) is None


def test_segment_speaker_correction_preserves_timed_words_and_raw_asr(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    calls = _mute_reindex(monkeypatch)
    from localplaud.config import get_settings
    from localplaud.db.models import (
        Chunk,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
        TranscriptRevision,
    )
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _load_transcript

    response = c.post(
        "/file/r1/transcript/segments/0",
        data={
            "text": "hello team",
            "speaker": "SPEAKER_01",
            "base_revision": 0,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with session_scope() as session:
        revision = session.scalar(select(TranscriptRevision))
        raw = session.scalar(select(Transcript).where(Transcript.file_id == "r1"))
        assert revision.kind == "speaker_edit"
        assert revision.note == (
            "reassigned segment 0 from SPEAKER_00 to SPEAKER_01"
        )
        assert revision.segments[0]["speaker"] == "SPEAKER_01"
        assert [word["speaker"] for word in revision.segments[0]["words"]] == [
            "SPEAKER_01",
            "SPEAKER_01",
        ]
        assert [word["start"] for word in revision.segments[0]["words"]] == [1.0, 1.5]
        assert [word["confidence"] for word in revision.segments[0]["words"]] == [
            0.91,
            0.88,
        ]
        assert raw.segments[0]["speaker"] == "SPEAKER_00"
        assert raw.segments[0]["words"][0]["speaker"] == "SPEAKER_00"
        assert session.query(Chunk).filter_by(file_id="r1").count() == 0
        runs = {
            row.stage: row
            for row in session.scalars(select(StageRun).where(StageRun.file_id == "r1"))
        }
        for stage in (StageName.summarize, StageName.mind_map, StageName.index):
            assert runs[stage].status == StageStatus.pending
            assert runs[stage].detail["stale"] is True

    transcript, source = _load_transcript("r1", get_settings())
    assert source == "local"
    assert transcript.segments[0].speaker == "SPEAKER_01"
    assert [word.speaker for word in transcript.segments[0].words] == [
        "SPEAKER_01",
        "SPEAKER_01",
    ]
    exported = c.get(
        "/file/r1/export/transcript.txt?timestamps=false&speakers=true"
    ).text
    assert "SPEAKER_01: hello team" in exported

    deadline = time.monotonic() + 2
    while calls != ["r1"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == ["r1"]


def test_segment_speaker_correction_validates_key_noop_and_unassignment(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    _mute_reindex(monkeypatch)
    from localplaud.db.models import Chunk, TranscriptRevision
    from localplaud.db.session import session_scope

    unknown = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "SPEAKER_99", "base_revision": 0},
    )
    assert unknown.status_code == 400
    no_change = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "SPEAKER_00", "base_revision": 0},
        follow_redirects=False,
    )
    assert no_change.status_code == 303
    with session_scope() as session:
        assert session.query(TranscriptRevision).count() == 0
        assert session.query(Chunk).filter_by(file_id="r1").count() == 1

    assigned = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "SPEAKER_01", "base_revision": 0},
        headers={"accept": "application/json"},
    )
    assert assigned.json() == {"changed": True, "revision": 1, "speaker": "SPEAKER_01"}
    unassigned = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "__none__", "base_revision": 1},
        headers={"accept": "application/json"},
    )
    assert unassigned.json() == {"changed": True, "revision": 2, "speaker": None}
    with session_scope() as session:
        latest = session.scalar(
            select(TranscriptRevision).order_by(TranscriptRevision.revision.desc()).limit(1)
        )
        assert latest.revision == 2
        assert latest.segments[0]["speaker"] is None
        assert all(word["speaker"] is None for word in latest.segments[0]["words"])
        assert latest.has_speakers is True  # segment 1 is still attributed


def test_last_speaker_occurrence_can_be_assigned_back_from_raw_or_durable_identity(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    from localplaud.db.models import Speaker, TranscriptRevision
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(Speaker(file_id="r1", key="SPEAKER_02", display_name="Observer"))
    first = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "SPEAKER_01", "base_revision": 0},
        headers={"accept": "application/json"},
    )
    assert first.status_code == 200

    picker = c.get("/file/r1/transcript-page?view=corrected")
    assert '<option value="SPEAKER_00" ' in picker.text  # immutable raw lane
    assert '<option value="SPEAKER_02" >Observer</option>' in picker.text  # durable row
    restored = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "SPEAKER_00", "base_revision": 1},
        headers={"accept": "application/json"},
    )
    assert restored.status_code == 200
    durable = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "SPEAKER_02", "base_revision": 2},
        headers={"accept": "application/json"},
    )
    assert durable.status_code == 200
    with session_scope() as session:
        revisions = list(
            session.scalars(select(TranscriptRevision).order_by(TranscriptRevision.revision))
        )
        assert [row.segments[0]["speaker"] for row in revisions] == [
            "SPEAKER_01",
            "SPEAKER_00",
            "SPEAKER_02",
        ]


def test_matching_segment_speaker_normalizes_mismatched_nested_words(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    from localplaud.db.models import Transcript, TranscriptRevision
    from localplaud.db.session import session_scope

    with session_scope() as session:
        raw = session.scalar(select(Transcript).where(Transcript.file_id == "r1"))
        segments = list(raw.segments)
        segments[0] = dict(segments[0])
        segments[0]["words"] = [dict(word) for word in segments[0]["words"]]
        segments[0]["words"][0]["speaker"] = "SPEAKER_01"
        raw.segments = segments
    response = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "SPEAKER_00", "base_revision": 0},
        headers={"accept": "application/json"},
    )
    assert response.json() == {
        "changed": True,
        "revision": 1,
        "speaker": "SPEAKER_00",
    }
    with session_scope() as session:
        revision = session.scalar(select(TranscriptRevision))
        assert revision.kind == "speaker_edit"
        assert revision.note == "normalized segment 0 word speakers as SPEAKER_00"
        assert [word["speaker"] for word in revision.segments[0]["words"]] == [
            "SPEAKER_00",
            "SPEAKER_00",
        ]
        assert [word["start"] for word in revision.segments[0]["words"]] == [1.0, 1.5]
        assert [word["confidence"] for word in revision.segments[0]["words"]] == [
            0.91,
            0.88,
        ]


def test_text_and_speaker_edit_is_one_atomic_revision(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    from localplaud.db.models import Transcript, TranscriptRevision
    from localplaud.db.session import session_scope

    response = c.post(
        "/file/r1/transcript/segments/0",
        data={
            "text": "hello, corrected team",
            "speaker": "SPEAKER_01",
            "base_revision": 0,
        },
        headers={"accept": "application/json"},
    )
    assert response.json() == {
        "changed": True,
        "revision": 1,
        "speaker": "SPEAKER_01",
    }
    with session_scope() as session:
        revision = session.scalar(select(TranscriptRevision))
        raw = session.scalar(select(Transcript).where(Transcript.file_id == "r1"))
        assert session.query(TranscriptRevision).count() == 1
        assert revision.kind == "speaker_edit"
        assert revision.note == (
            "edited text and reassigned segment 0 from SPEAKER_00 to SPEAKER_01"
        )
        assert revision.segments[0] == {
            "text": "hello, corrected team",
            "start": 1.0,
            "end": 2.0,
            "speaker": "SPEAKER_01",
            "words": [],
        }
        assert raw.segments == SEGMENTS


def test_speaker_correction_is_rejected_while_processing_without_mutation(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    _mute_reindex(monkeypatch)
    from localplaud.db.models import Chunk, PlaudFile, TranscriptRevision
    from localplaud.db.session import session_scope

    with session_scope() as session:
        recording = session.get(PlaudFile, "r1")
        recording.processing_token = "active-claim"
        recording.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)
    response = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "SPEAKER_01", "base_revision": 0},
        headers={"accept": "application/json"},
    )
    assert response.status_code == 409
    assert response.json()["error"] == "recording is processing; try again when it finishes"
    with session_scope() as session:
        assert session.query(TranscriptRevision).count() == 0
        assert session.query(Chunk).filter_by(file_id="r1").count() == 1


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_concurrent_speaker_corrections_have_one_winner_and_one_conflict(
    monkeypatch, tmp_path, journal_mode
):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    from localplaud.db.models import TranscriptRevision
    from localplaud.db.session import get_engine, session_scope

    with get_engine().connect() as connection:
        selected_mode = connection.exec_driver_sql(
            f"PRAGMA journal_mode={journal_mode}"
        ).scalar_one()
    assert selected_mode.lower() == journal_mode

    barrier = Barrier(3)

    def submit(segment: int, speaker: str):
        barrier.wait()
        return c.post(
            f"/file/r1/transcript/segments/{segment}",
            data={
                "text": SEGMENTS[segment]["text"],
                "speaker": speaker,
                "base_revision": 0,
            },
            headers={"accept": "application/json"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(submit, 0, "SPEAKER_01")
        second = pool.submit(submit, 1, "SPEAKER_00")
        barrier.wait()
        responses = [first.result(timeout=5), second.result(timeout=5)]

    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response for response in responses if response.status_code == 200)
    conflict = next(response for response in responses if response.status_code == 409)
    assert winner.json()["revision"] == 1
    assert conflict.json()["error"] == "transcript changed; reload before saving"
    with session_scope() as session:
        revisions = list(session.scalars(select(TranscriptRevision)))
        assert len(revisions) == 1
        assert revisions[0].revision == 1


def test_segment_editor_lists_stable_speakers_and_restores_focus_contract(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import Speaker
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            Speaker(file_id="r1", key="SPEAKER_01", display_name="Facilitator")
        )
    transcript = c.get("/file/r1/transcript-page?view=raw")
    assert transcript.status_code == 200
    assert 'aria-label="Edit segment text and speaker"' in transcript.text
    assert '<select class="search-input" name="speaker" aria-label="Speaker">' in transcript.text
    assert '<option value="__none__"' in transcript.text
    assert '<option value="SPEAKER_00" selected>SPEAKER_00</option>' in transcript.text
    assert '<option value="SPEAKER_01" >Facilitator</option>' in transcript.text
    detail = c.get("/file/r1")
    assert "form.parentElement.querySelector('.editbtn')?.focus()" in detail.text
    assert "event.target.closest?.('.seg .segedit')" in detail.text
    assert "const resetSegmentEditor=form=>{form.reset()" in detail.text
    assert "form.querySelector('[data-segment-status]').textContent=''" in detail.text
    assert "form.querySelector('[type=\"submit\"]').disabled=false" in detail.text
    assert "headers:{accept:'application/json'}" in detail.text
    assert "url.searchParams.set('tab','transcript')" in detail.text
    assert "else url.searchParams.delete('t')" in detail.text
    assert "recording_processing:'Recording is processing." in detail.text
    assert "document.querySelector('[data-segment-edit-error]')?.focus()" in detail.text
    assert ".seg:focus-within .editbtn" in detail.text
    assert ".seg .editbtn:focus-visible" in detail.text
    assert ".seg .editbtn { opacity:0" in detail.text


def test_native_segment_form_preserves_workspace_and_has_accessible_error_recovery(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    context = {
        "return_to": "/?q=weekly&state=done&page=2",
        "tab": "transcript",
        "view": "raw",
        "t": "147",
    }
    partial = c.get(
        "/file/r1/transcript-page",
        params=context | {"view": "raw"},
    )
    assert 'name="return_to" value="/?q=weekly&amp;state=done&amp;page=2"' in partial.text
    assert 'name="tab" value="transcript"' in partial.text
    assert 'name="view" value="raw"' in partial.text
    assert 'name="t" value="147"' in partial.text

    saved = c.post(
        "/file/r1/transcript/segments/0",
        data=context
        | {"text": "native correction", "speaker": "SPEAKER_00", "base_revision": 0},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == (
        "/file/r1?return_to=%2F%3Fq%3Dweekly%26state%3Ddone%26page%3D2"
        "&tab=transcript&view=corrected&t=147"
    )

    no_op = c.post(
        "/file/r1/transcript/segments/0",
        data=context
        | {"text": "native correction", "speaker": "SPEAKER_00", "base_revision": 1},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert no_op.headers["location"].endswith("&tab=transcript&view=raw&t=147")

    stale = c.post(
        "/file/r1/transcript/segments/0",
        data=context
        | {"text": "stale correction", "speaker": "SPEAKER_00", "base_revision": 0},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert stale.status_code == 303
    assert "segment_error=stale_revision" in stale.headers["location"]
    recovery = c.get(stale.headers["location"])
    assert 'role="alert"' in recovery.text
    assert "Could not save transcript correction" in recovery.text
    assert "Transcript changed. Reload before saving." in recovery.text


def test_postgresql_transcript_mutation_locks_recording_row_before_claim_check():
    from sqlalchemy.dialects import postgresql

    from localplaud.api.app import _serialize_transcript_mutation

    statements: list[str] = []

    class Bind:
        class dialect:
            name = "postgresql"

    class Session:
        def get_bind(self):
            return Bind()

        def execute(self, statement):
            statements.append(str(statement))

        def scalar(self, statement):
            statements.append(
                str(
                    statement.compile(
                        dialect=postgresql.dialect(),
                        compile_kwargs={"literal_binds": True},
                    )
                )
            )
            return None

    _serialize_transcript_mutation(Session(), "recording")
    assert statements[0] == "SELECT pg_advisory_xact_lock_shared(1280330574)"
    assert "plaud_files.id = 'recording'" in statements[1]
    assert "FOR UPDATE" in statements[1]


@pytest.mark.parametrize("offset", ["inf", "-inf", "Infinity", "1e309"])
def test_non_finite_playback_offsets_are_discarded(monkeypatch, tmp_path, offset):
    c = _client(monkeypatch, tmp_path)
    _seed()

    detail = c.get("/file/r1", params={"t": offset})
    partial = c.get("/file/r1/transcript-page", params={"t": offset})

    assert detail.status_code == 200
    assert partial.status_code == 200
    assert 'name="t" value=""' in partial.text
    assert "Number.isFinite(s)&&s>=0" in detail.text


def test_segment_conflicts_have_distinct_structured_codes_and_native_processing_recovery(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "first", "base_revision": 0},
        headers={"accept": "application/json"},
    )
    stale = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "second", "base_revision": 0},
        headers={"accept": "application/json"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_revision"

    with session_scope() as session:
        recording = session.get(PlaudFile, "r1")
        recording.processing_token = "active-claim"
        recording.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)
    processing = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "third", "base_revision": 1},
        headers={"accept": "application/json"},
    )
    assert processing.status_code == 409
    assert processing.json()["code"] == "recording_processing"
    native = c.post(
        "/file/r1/transcript/segments/0",
        data={
            "text": "third",
            "base_revision": 1,
            "return_to": "/?tag=4",
            "tab": "transcript",
            "view": "corrected",
            "t": "22",
        },
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert native.status_code == 303
    assert "segment_error=recording_processing" in native.headers["location"]
    recovery = c.get(native.headers["location"])
    assert "Recording is processing. Wait until it finishes before editing." in recovery.text


def test_speaker_revision_reason_is_localized_in_traditional_chinese(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    response = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "SPEAKER_01", "base_revision": 0},
        headers={"accept": "application/json"},
    )
    assert response.status_code == 200
    preferences = c.get("/api/preferences/workspace").json()
    assert c.put(
        "/api/preferences/workspace",
        json=preferences | {"locale": "zh-Hant-TW"},
    ).status_code == 200
    history = c.get("/file/r1?view=corrected")
    assert "重新指定段落講者 1: SPEAKER_00 → SPEAKER_01" in history.text
    assert "reassigned segment 0 from SPEAKER_00 to SPEAKER_01" not in history.text


def test_unassigned_speaker_revision_reason_is_fully_localized(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    response = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "__none__", "base_revision": 0},
        headers={"accept": "application/json"},
    )
    assert response.status_code == 200
    preferences = c.get("/api/preferences/workspace").json()
    assert c.put(
        "/api/preferences/workspace",
        json=preferences | {"locale": "zh-Hant-TW"},
    ).status_code == 200

    history = c.get("/file/r1?view=corrected")
    assert "重新指定段落講者 1: SPEAKER_00 → 未指定講者" in history.text
    assert "unassigned" not in history.text


def test_speaker_correction_reindexes_ask_chunks_with_revision_lineage(
    monkeypatch, tmp_path
):
    from localplaud.worker.reindex import reindex_file

    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    _mute_reindex(monkeypatch)
    response = c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "SPEAKER_01", "base_revision": 0},
        headers={"accept": "application/json"},
    )
    assert response.json()["revision"] == 1

    import localplaud.worker.index as index_mod
    from localplaud.db.models import Chunk
    from localplaud.db.session import session_scope

    monkeypatch.setattr(
        index_mod,
        "embed_chunks",
        lambda chunks, settings: ([b"\x00\x00\x80?" for _ in chunks], "fake", 1),
    )
    assert reindex_file("r1", expected_revision=1) is True
    with session_scope() as session:
        chunks = list(
            session.scalars(select(Chunk).where(Chunk.file_id == "r1").order_by(Chunk.idx))
        )
        assert chunks[0].text == "hello team let's start"
        assert chunks[0].speaker == "SPEAKER_01"
        assert chunks[0].input_transcript_revision == 1
        assert chunks[0].input_transcript_source == "local"


def test_speaker_attribution_revision_can_be_restored_without_changing_raw(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    c.post(
        "/file/r1/transcript/segments/0",
        data={"text": "hello team", "speaker": "SPEAKER_01", "base_revision": 0},
    )
    c.post(
        "/file/r1/transcript/segments/1",
        data={"text": "let us begin", "base_revision": 1},
    )
    restored = c.post(
        "/file/r1/transcript/revisions/1/restore",
        data={"base_revision": 2},
        follow_redirects=False,
    )
    assert restored.status_code == 303

    from localplaud.db.models import Transcript, TranscriptRevision
    from localplaud.db.session import session_scope

    with session_scope() as session:
        latest = session.scalar(
            select(TranscriptRevision).order_by(TranscriptRevision.revision.desc()).limit(1)
        )
        raw = session.scalar(select(Transcript).where(Transcript.file_id == "r1"))
        assert latest.revision == 3
        assert latest.note == "restored revision 1"
        assert latest.segments[0]["speaker"] == "SPEAKER_01"
        assert latest.segments[0]["words"][0]["speaker"] == "SPEAKER_01"
        assert raw.segments[0]["speaker"] == "SPEAKER_00"


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
    assert "Revision 1 preview" in preview.text
    preview_transcript = c.get(
        "/file/r1/transcript-page?view=corrected&revision=1"
    ).text
    assert "first version" in preview_transcript
    assert "second version" not in preview_transcript
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


def test_repair_emptied_polish_revision_is_immutable_stale_and_idempotent(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    calls = _mute_reindex(monkeypatch)
    from localplaud.db.models import (
        Chunk,
        StageName,
        StageRun,
        StageStatus,
        Summary,
        Transcript,
        TranscriptRevision,
    )
    from localplaud.db.session import session_scope

    with session_scope() as session:
        raw = session.scalar(select(Transcript).where(Transcript.file_id == "r1"))
        session.add(
            TranscriptRevision(
                file_id="r1",
                base_transcript_id=raw.id,
                revision=1,
                source="local",
                segments=[dict(SEGMENTS[0]) | {"text": ""}, dict(SEGMENTS[1])],
                text="let's start",
                has_speakers=True,
                note="AI polished",
                kind="ai_polish",
            )
        )
        session.add(
            Summary(
                file_id="r1",
                template="meeting",
                content_md="# Existing note",
                source="local",
            )
        )
        session.add(
            StageRun(
                file_id="r1",
                stage=StageName.summarize,
                status=StageStatus.completed,
                attempts=1,
            )
        )

    repaired = c.post("/api/files/r1/transcript/repair-empty-polish")
    assert repaired.status_code == 200
    assert repaired.json() == {"repaired": True, "restored_segments": 1, "revision": 2}
    second = c.post("/api/files/r1/transcript/repair-empty-polish")
    assert second.json() == {"repaired": False, "restored_segments": 0, "revision": None}

    with session_scope() as session:
        raw = session.scalar(select(Transcript).where(Transcript.file_id == "r1"))
        revisions = list(
            session.scalars(
                select(TranscriptRevision).order_by(TranscriptRevision.revision)
            )
        )
        summarize = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.summarize
            )
        )
        assert len(revisions) == 2
        assert revisions[0].segments[0]["text"] == ""
        assert revisions[1].segments[0] == SEGMENTS[0]
        assert revisions[1].segments[1] == SEGMENTS[1]
        assert revisions[1].note == "restored 1 segments from raw ASR"
        assert raw.segments == SEGMENTS
        assert session.query(Chunk).filter_by(file_id="r1").count() == 0
        assert summarize.status == StageStatus.pending
        assert summarize.detail["stale"] is True
    assert calls == ["r1"]


def test_repair_emptied_polish_revision_leaves_undamaged_revision_untouched(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _mute_reindex(monkeypatch)
    from localplaud.db.models import Transcript, TranscriptRevision
    from localplaud.db.session import session_scope

    with session_scope() as session:
        raw = session.scalar(select(Transcript).where(Transcript.file_id == "r1"))
        session.add(
            TranscriptRevision(
                file_id="r1",
                base_transcript_id=raw.id,
                revision=1,
                source="local",
                segments=SEGMENTS,
                text=raw.text,
                has_speakers=True,
                note="AI polished",
                kind="ai_polish",
            )
        )

    response = c.post("/api/files/r1/transcript/repair-empty-polish")
    assert response.json() == {
        "repaired": False,
        "restored_segments": 0,
        "revision": None,
    }
    with session_scope() as session:
        assert session.query(TranscriptRevision).count() == 1


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
    assert 'href="/file/r1?view=raw"' not in page.text
    assert 'class="editbtn"' in c.get("/file/r1/transcript-page?view=raw").text

    c.post("/file/r1/transcript/segments/0", data={"text": "hello, team!", "base_revision": 0},
           follow_redirects=False)

    # default view is now the corrected canonical transcript
    page = c.get("/file/r1")
    corrected_transcript = c.get("/file/r1/transcript-page?view=corrected").text
    assert "hello, team!" in corrected_transcript
    assert "Corrected (rev 1)" in page.text  # labelled current view
    assert (
        'href="/file/r1?view=raw&amp;return_to=%2F"' in page.text
    )  # toggle preserves the library return context
    assert 'class="editbtn"' in corrected_transcript

    # explicit raw view shows the untouched ASR output, read-only
    raw = c.get("/file/r1?view=raw")
    raw_transcript = c.get("/file/r1/transcript-page?view=raw").text
    assert "hello team" in raw_transcript
    assert "hello, team!" not in raw_transcript
    assert "?view=corrected" in raw.text
    assert 'class="editbtn"' not in raw_transcript  # no edits from the raw view


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
