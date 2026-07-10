"""Stable speaker identities: sync, rename endpoint, UI fallback, export."""

from __future__ import annotations

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
    {"text": "hi there", "start": 2.0, "end": 3.0, "speaker": "SPEAKER_01", "words": []},
    {"text": "sounds good", "start": 3.0, "end": 4.0, "speaker": "SPEAKER_00", "words": []},
]


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


def test_rename_endpoint_upsert_clear_and_validation(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    Speaker = _seed()
    from localplaud.db.session import session_scope

    def name_of(key):
        with session_scope() as s:
            row = s.scalar(select(Speaker).where(Speaker.file_id == "r1", Speaker.key == key))
            return row.display_name if row else "<missing>"

    r = c.post("/file/r1/speakers", data={"key": "SPEAKER_00", "name": "Alice"},
               follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/file/r1"
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


def test_detail_page_shows_display_name_and_falls_back_to_key(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
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
    _seed()
    c.post("/file/r1/speakers", data={"key": "SPEAKER_00", "name": "Alice"},
           follow_redirects=False)
    md = c.get("/file/r1/export.md").text
    assert "**[00:01] Alice:** hello team" in md
    assert "**[00:02] SPEAKER_01:** hi there" in md  # unnamed key unchanged
    assert "SPEAKER_00" not in md
