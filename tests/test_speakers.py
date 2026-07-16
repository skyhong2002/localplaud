"""Stable speaker identities: sync, rename endpoint, UI fallback, export."""

from __future__ import annotations

import time

from sqlalchemy import create_engine, inspect, select, text


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
    {"text": "hi there", "start": 2.0, "end": 3.0, "speaker": "SPEAKER_01", "words": []},
    {"text": "sounds good", "start": 3.0, "end": 4.0, "speaker": "SPEAKER_00", "words": []},
]


def test_speaker_timeline_migration_is_idempotent(tmp_path):
    from localplaud.db.migrations import migrate_speaker_timeline_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE speakers (id INTEGER PRIMARY KEY, file_id VARCHAR(64), "
                "key VARCHAR(64), display_name VARCHAR(128))"
            )
        )
    assert migrate_speaker_timeline_schema(engine) == ["speakers.timeline"]
    assert migrate_speaker_timeline_schema(engine) == []
    assert "timeline" in {
        column["name"] for column in inspect(engine).get_columns("speakers")
    }


def _mute_reindex(monkeypatch):
    import localplaud.worker.reindex as reindex_mod

    calls = []
    monkeypatch.setattr(
        reindex_mod,
        "reindex_file",
        lambda file_id, settings=None, **kwargs: calls.append((file_id, kwargs)),
    )
    return calls


def _seed(file_id: str = "r1"):
    from localplaud.db.models import FileStatus, PlaudFile, Speaker, Transcript
    from localplaud.db.session import session_scope
    from localplaud.store.speakers import speaker_keys_from_segments, sync_speakers

    with session_scope() as s:
        s.add(PlaudFile(id=file_id, filename="Weekly Sync", status=FileStatus.done,
                        duration_ms=600000, start_time_ms=1783582737000))
        s.add(Transcript(file_id=file_id, provider="faster-whisper", language="en",
                         has_speakers=True, source="local",
                         text="hello team\nhi there\nsounds good", segments=SEGMENTS))
        sync_speakers(s, file_id, speaker_keys_from_segments(SEGMENTS))
    return Speaker


def test_speaker_keys_from_segments_order_and_words():
    from localplaud.store.speakers import speaker_keys_from_segments

    segments = [
        {"text": "a", "speaker": "SPEAKER_01",
         "words": [{"text": "a", "speaker": "SPEAKER_02"}]},
        {"text": "b", "speaker": "SPEAKER_00"},
        {"text": "c", "speaker": "SPEAKER_01", "words": None},
        {"text": "d", "speaker": None},
    ]
    assert speaker_keys_from_segments(segments) == ["SPEAKER_01", "SPEAKER_02", "SPEAKER_00"]
    assert speaker_keys_from_segments([]) == []


def test_sync_preserves_display_names_across_repersist(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    Speaker = _seed()
    from localplaud.asr.base import Segment, Transcript
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _persist_transcript

    with session_scope() as s:
        row = s.scalar(select(Speaker).where(Speaker.file_id == "r1",
                                             Speaker.key == "SPEAKER_00"))
        row.display_name = "Alice"

    # Re-run ASR persistence (e.g. rebuild) — same diarization keys come back.
    _persist_transcript(
        "r1",
        Transcript(
            segments=[
                Segment(text="hello again", start=0.0, end=1.0, speaker="SPEAKER_00"),
                Segment(text="new voice", start=1.0, end=2.0, speaker="SPEAKER_02"),
            ],
            provider="faster-whisper",
            has_speakers=True,
        ),
    )
    with session_scope() as s:
        rows = {r.key: r.display_name for r in s.scalars(
            select(Speaker).where(Speaker.file_id == "r1").order_by(Speaker.id))}
    # rename preserved, old keys never deleted, new key inserted without a name
    assert rows == {"SPEAKER_00": "Alice", "SPEAKER_01": None, "SPEAKER_02": None}


def test_diarization_rerun_reconciles_swapped_labels_by_timeline(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    Speaker = _seed()
    from localplaud.asr.base import Segment, Transcript
    from localplaud.db.models import Transcript as TranscriptRow
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _persist_transcript

    with session_scope() as s:
        alice = s.scalar(
            select(Speaker).where(
                Speaker.file_id == "r1", Speaker.key == "SPEAKER_00"
            )
        )
        alice.display_name = "Alice"

    # Rebuild first stores a fresh ASR transcript without speakers. Persisting it
    # captures the previous diarization evidence before replacing the old row.
    _persist_transcript(
        "r1",
        Transcript(
            segments=[Segment(text="all speech", start=1.0, end=4.0)],
            provider="faster-whisper",
        ),
    )
    # The new diarizer numbers both voices in the opposite order.
    _persist_transcript(
        "r1",
        Transcript(
            segments=[
                Segment(text="hello team", start=1.0, end=2.0, speaker="SPEAKER_01"),
                Segment(text="hi there", start=2.0, end=3.0, speaker="SPEAKER_00"),
                Segment(text="sounds good", start=3.0, end=4.0, speaker="SPEAKER_01"),
            ],
            provider="faster-whisper",
            has_speakers=True,
        ),
    )
    with session_scope() as s:
        transcript = s.scalar(
            select(TranscriptRow).where(
                TranscriptRow.file_id == "r1", TranscriptRow.source == "local"
            )
        )
        names = {
            row.key: row.display_name
            for row in s.scalars(select(Speaker).where(Speaker.file_id == "r1"))
        }
    assert [segment["speaker"] for segment in transcript.segments] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "SPEAKER_00",
    ]
    assert names["SPEAKER_00"] == "Alice"


def test_ambiguous_rerun_voice_gets_new_identity_instead_of_name(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    Speaker = _seed()
    from localplaud.db.session import session_scope
    from localplaud.store.speakers import capture_speaker_evidence, reconcile_speaker_labels

    with session_scope() as s:
        alice = s.scalar(
            select(Speaker).where(
                Speaker.file_id == "r1", Speaker.key == "SPEAKER_00"
            )
        )
        alice.display_name = "Alice"
        capture_speaker_evidence(s, "r1", SEGMENTS)
        bob = s.scalar(
            select(Speaker).where(
                Speaker.file_id == "r1", Speaker.key == "SPEAKER_01"
            )
        )
        alice.timeline = {"intervals": [[1.0, 2.0]]}
        bob.timeline = {"intervals": [[2.0, 3.0]]}
        # One new label covers equal portions of two old voices; assigning either
        # name would be unsafe, so it must become a fresh stable identity.
        ambiguous = [
            {
                "text": "mixed",
                "start": 1.0,
                "end": 3.0,
                "speaker": "SPEAKER_00",
                "words": [],
            }
        ]
        mapping = reconcile_speaker_labels(s, "r1", ambiguous)
        new_key = mapping["SPEAKER_00"]
        assert new_key not in {"SPEAKER_00", "SPEAKER_01"}
        assert ambiguous[0]["speaker"] == new_key
        new_row = s.scalar(
            select(Speaker).where(Speaker.file_id == "r1", Speaker.key == new_key)
        )
        assert new_row.display_name is None


def test_rename_endpoint_upsert_clear_and_validation(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _mute_reindex(monkeypatch)
    Speaker = _seed()
    from localplaud.db.session import session_scope

    def name_of(key):
        with session_scope() as s:
            row = s.scalar(select(Speaker).where(Speaker.file_id == "r1", Speaker.key == key))
            return row.display_name if row else "<missing>"

    r = c.post("/file/r1/speakers", data={"key": "SPEAKER_00", "name": "Alice"},
               follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/file/r1?return_to=%2F&tab=transcript"
    assert name_of("SPEAKER_00") == "Alice"

    # empty/whitespace clears the name back to the stable key
    r = c.post("/file/r1/speakers", data={"key": "SPEAKER_00", "name": "   "},
               follow_redirects=False)
    assert r.status_code == 303
    assert name_of("SPEAKER_00") is None

    # unknown key on a known file is rejected
    assert c.post("/file/r1/speakers", data={"key": "SPEAKER_99", "name": "X"},
                  follow_redirects=False).status_code == 400
    # unknown file
    assert c.post("/file/nope/speakers", data={"key": "SPEAKER_00", "name": "X"},
                  follow_redirects=False).status_code == 404


def test_speaker_rename_keeps_durable_reindex_queue_when_thread_start_fails(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope

    eager_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "localplaud.api.app._start_transcript_reindex",
        lambda file_id, **kwargs: eager_calls.append((file_id, kwargs)) and False,
    )
    response = c.post(
        "/file/r1/speakers",
        data={"key": "SPEAKER_00", "name": "Alice"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert eager_calls == [
        (
            "r1",
            {
                "expected_revision": 0,
                "expected_speaker_names": {"SPEAKER_00": "Alice"},
            },
        )
    ]
    with session_scope() as session:
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.index
            )
        )
        assert run.status == StageStatus.pending
        assert run.detail["reindex_only"] is True
        assert run.detail["reason"] == "canonical transcript changed"


def test_speaker_rename_does_not_eagerly_index_when_stage_is_disabled(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.config import get_settings
    from localplaud.db.models import StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope

    settings = get_settings().model_copy(deep=True)
    settings.pipeline.index = False
    monkeypatch.setattr("localplaud.api.app.get_settings", lambda: settings)
    calls = _mute_reindex(monkeypatch)

    response = c.post(
        "/file/r1/speakers",
        data={"key": "SPEAKER_00", "name": "Alice"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    time.sleep(0.02)
    assert calls == []
    with session_scope() as session:
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.index
            )
        )
        assert run.status == StageStatus.pending
        assert run.detail["reindex_only"] is True


def test_detail_page_shows_display_name_and_falls_back_to_key(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _mute_reindex(monkeypatch)
    _seed()
    c.post("/file/r1/speakers", data={"key": "SPEAKER_00", "name": "Alice"},
           follow_redirects=False)
    page = c.get("/file/r1")
    assert page.status_code == 200
    assert "Alice" in page.text  # renamed speaker label
    assert "SPEAKER_01" in page.text  # unnamed speaker falls back to the key
    # the legend keeps the stable key visible as the input placeholder
    assert 'placeholder="SPEAKER_00"' in page.text
    assert 'value="Alice"' in page.text
    # swatches keep stable coloring hooks
    assert 'swatch" data-spk="SPEAKER_00"' in page.text


def test_export_uses_display_names(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _mute_reindex(monkeypatch)
    _seed()
    c.post("/file/r1/speakers", data={"key": "SPEAKER_00", "name": "Alice"},
           follow_redirects=False)
    md = c.get("/file/r1/export.md").text
    assert "**[00:01] Alice:** hello team" in md
    assert "**[00:02] SPEAKER_01:** hi there" in md  # unnamed key unchanged
    assert "SPEAKER_00" not in md


def test_rename_invalidates_derived_artifacts_and_names_canonical(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    calls = _mute_reindex(monkeypatch)
    _seed()

    from localplaud.config import get_settings
    from localplaud.db.models import Chunk, PlaudFile, StageName, StageStatus, Summary
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _load_transcript

    with session_scope() as s:
        s.add(Chunk(file_id="r1", idx=0, text="old index"))
        s.add(Summary(file_id="r1", template="default", source="local", content_md="old"))

    response = c.post(
        "/file/r1/speakers",
        data={"key": "SPEAKER_00", "name": "Alice"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    transcript, source = _load_transcript("r1", get_settings())
    assert source == "local"
    assert transcript.segments[0].speaker == "Alice"
    stable_transcript, stable_source = _load_transcript(
        "r1", get_settings(), display_speaker_names=False
    )
    assert stable_source == "local"
    assert stable_transcript.segments[0].speaker == "SPEAKER_00"
    with session_scope() as s:
        row = s.get(PlaudFile, "r1")
        assert row.chunks == []
        runs = {run.stage: run for run in row.stage_runs}
        for stage in (StageName.summarize, StageName.mind_map, StageName.index):
            assert runs[stage].status == StageStatus.pending
            assert runs[stage].detail["stale"] is True

    deadline = time.monotonic() + 2
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls and calls[0][0] == "r1"
    assert calls[0][1]["expected_speaker_names"] == {"SPEAKER_00": "Alice"}
