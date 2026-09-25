"""Voice identity is opt-in, traceable, conservative and independent of Plaud AI."""

import copy
import math

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from localplaud.db.models import Base, FileStatus, PlaudFile, Speaker, StageRun, Transcript
from localplaud.voice_identity import (
    VoiceAssignment,
    VoiceEvent,
    apply_match,
    create_schema,
    inventory,
    references,
    undo_assignment,
)
from localplaud.voice_matching import clean_windows, cosine, match_voice, usable_name


@pytest.mark.parametrize(
    "name", ["", "Speaker 1", "SPEAKER_01", "說話人 2", "1#", "Sky / Grace", "甲、乙", "A & B"]
)
def test_anonymous_and_combined_names_are_not_references(name):
    assert not usable_name(name)


def test_clean_speech_excludes_other_speakers_and_invalid_times():
    windows = clean_windows(
        [
            {"speaker": "a", "start": 0, "end": 30},
            {"speaker": "b", "start": 10, "end": 20},
            {"speaker": "x", "start": -1, "end": 9},
            {"speaker": "x", "start": 0, "end": math.inf},
        ]
    )
    assert "x" not in windows
    for start, end in windows["a"]:
        assert end <= 10 or start >= 20
    # Overlapping speech is unsuitable for BOTH speakers.
    assert "b" not in windows


def test_words_override_misleading_segment_speaker():
    windows = clean_windows(
        [
            {
                "speaker": "wrong",
                "start": 0,
                "end": 20,
                "words": [
                    {"speaker": "a", "start": 0, "end": 8},
                    {"speaker": "b", "start": 8, "end": 20},
                ],
            }
        ]
    )
    assert set(windows) == {"a", "b"}


def refs(name="Alice", vector=None, count=2):
    return [
        {
            "name": name,
            "file_id": f"{name}-{i}",
            "speaker_key": "s",
            "source": "local",
            "vectors": [vector or [1.0, 0.0], vector or [1.0, 0.0]],
        }
        for i in range(count)
    ]


def test_requires_independent_recordings_and_multiple_query_windows():
    query = [[1.0, 0.0], [1.0, 0.0]]
    assert match_voice(query, refs(count=1))["status"] == "ambiguous"
    assert match_voice(query[:1], refs())["status"] == "insufficient"
    assert match_voice(query, refs(), query_file_id="Alice-0")["status"] == "ambiguous"
    assert match_voice(query, refs())["status"] == "matched"


def test_similar_names_are_ambiguous_and_negative_is_unknown():
    assert (
        match_voice([[1.0, 0.0]] * 3, refs() + refs("Bob", [0.99, 0.01]))["status"] == "ambiguous"
    )
    assert match_voice([[0.0, 1.0]] * 3, refs())["status"] == "unknown"


def test_nonfinite_and_empty_vectors_rejected():
    with pytest.raises(ValueError):
        cosine([math.nan], [1.0])
    with pytest.raises(ValueError):
        match_voice([[0.0, 0.0]] * 2, refs())


@pytest.fixture
def database(monkeypatch, tmp_path):
    # Existing transactional rename invalidation helpers are used unchanged.
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'identity.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    Base.metadata.create_all(engine)
    create_schema(engine)
    create_schema(engine)
    with Session(engine, expire_on_commit=False) as session:
        row = PlaudFile(id="local-only", filename="test", status=FileStatus.done)
        session.add(row)
        session.add(
            Transcript(
                file_id=row.id,
                source="local",
                provider="qwen",
                model="test",
                text="hello",
                segments=[
                    {"text": "hello", "speaker": "s", "start": 0.0, "end": 30.0, "words": []}
                ],
                has_speakers=True,
            )
        )
        speaker = Speaker(file_id=row.id, key="s")
        session.add(speaker)
        session.commit()
        yield session, row, speaker


def decision():
    return {
        "status": "matched",
        "name": "Alice",
        "score": 0.9,
        "runner_up": 0.1,
        "reference_ids": [["ref1", "a"], ["ref2", "b"]],
    }


def test_local_only_recording_reuses_voiceprint_and_invalidates_derived_artifacts(database):
    session, row, speaker = database
    samples = inventory(session, row)
    original = copy.deepcopy(row.local_transcript.segments)
    assert len(samples) == 1
    sample = samples[0]
    sample.vectors, sample.status = [[1.0, 0.0]] * 3, "ready"
    session.commit()
    assert inventory(session, row)[0].id == sample.id
    assert apply_match(session, sample, decision()) == "applied"
    session.commit()
    assert speaker.display_name == "Alice"
    assert row.local_transcript.segments == original
    assert len(list(session.scalars(select(VoiceEvent)))) == 1
    stages = list(session.scalars(select(StageRun)))
    assert {s.stage.value for s in stages} == {"summarize", "mind_map", "index"}
    assert all(s.detail["stale"] for s in stages)
    # Auto-named voice is never enrolled as a manual reference.
    assert references(inventory(session, row)) == []
    assert apply_match(session, sample, decision()) == "already_applied"
    assert undo_assignment(session, speaker.id)
    assert speaker.display_name is None
    assert apply_match(session, sample, decision()) == "manual"


def test_human_override_survives_and_becomes_reference(database):
    session, row, speaker = database
    sample = inventory(session, row)[0]
    sample.vectors, sample.status = [[1.0, 0.0]] * 2, "ready"
    apply_match(session, sample, decision())
    session.commit()
    speaker.display_name = "Bob"
    session.commit()
    assert inventory(session, row)[0].reference_name == "Bob"
    assert session.get(VoiceAssignment, speaker.id).status == "overridden"
    assert apply_match(session, sample, decision()) == "manual"
    assert not undo_assignment(session, speaker.id)
    assert speaker.display_name == "Bob"


def test_manual_names_and_active_claims_protected(database):
    from datetime import UTC, datetime, timedelta

    session, row, speaker = database
    sample = inventory(session, row)[0]
    speaker.display_name = "Human choice"
    assert apply_match(session, sample, decision()) == "manual"
    session.commit()
    row.processing_token = "active"
    row.processing_lease_until = datetime.now(UTC) + timedelta(minutes=10)
    session.commit()
    assert apply_match(session, sample, decision()) == "busy"


def test_import_is_opt_in_and_excludes_combined_labels(database):
    session, row, _speaker = database
    cloud = Transcript(
        file_id=row.id,
        source="cloud",
        provider="plaud",
        model="unknown",
        text="import",
        segments=[
            {"speaker": "Sky", "start": 0, "end": 15},
            {"speaker": "A/B", "start": 16, "end": 30},
        ],
    )
    row.transcripts.append(cloud)
    session.commit()
    assert {s.source for s in inventory(session, row)} == {"local"}
    samples = inventory(session, row, import_plaud=True)
    assert [(s.reference_name, s.source) for s in samples if s.source != "local"] == [
        ("Sky", "plaud-reference")
    ]


def test_changed_timeline_cannot_apply_stale_voice_match(database):
    session, row, speaker = database
    sample = inventory(session, row)[0]
    session.commit()
    row.local_transcript.segments = [
        {"text": "changed", "speaker": "s", "start": 40.0, "end": 80.0, "words": []}
    ]
    session.commit()
    assert apply_match(session, sample, decision()) == "stale"
    assert speaker.display_name is None


def test_packed_audio_contains_only_selected_windows(tmp_path):
    import io
    import struct
    import wave
    from types import SimpleNamespace

    from localplaud.voice_service import pack_windows

    original = tmp_path / "original.wav"
    with wave.open(str(original), "wb") as out:
        out.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        out.writeframes(struct.pack("<h", 1000) * 16000 * 10)
    before = original.read_bytes()
    data, windows, owners = pack_windows(
        original, [SimpleNamespace(id="a", windows=[[1, 4], [6, 9], [11, 15]])]
    )
    with wave.open(io.BytesIO(data)) as output:
        assert output.getnframes() == 16000 * 6
    assert windows == [[0, 3], [3, 6]]
    assert owners == ["a", "a"]
    assert original.read_bytes() == before


def test_voice_worker_rejects_wrong_response_and_resets_transport(monkeypatch, tmp_path):
    from io import StringIO
    from types import SimpleNamespace

    from localplaud.voice_service import VoiceWorker

    worker = VoiceWorker({}, tmp_path / "runtime.log")
    monkeypatch.setattr(worker, "start", lambda: None)
    worker.process = SimpleNamespace(stdin=StringIO())
    monkeypatch.setattr(worker, "read", lambda: {"request_id": "previous-recording"})
    reset = []
    monkeypatch.setattr(worker, "reset", lambda: reset.append(True))
    with pytest.raises(RuntimeError, match="request mismatch"):
        worker.embed(b"audio", [[0, 4]])
    assert reset == [True]
    worker.log.close()
