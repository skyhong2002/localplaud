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
from localplaud.voice_matching import (
    clean_windows,
    consistent_vectors,
    cosine,
    match_voice,
    usable_name,
)


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


def test_one_stray_window_no_longer_fails_a_consistent_voice():
    voice = [[1.0, 0.1], [1.0, 0.0], [1.0, -0.1], [0.9, 0.1], [1.0, 0.05]]
    stray = [0.0, 1.0]
    kept = consistent_vectors([*voice[:3], stray, *voice[3:]])
    assert kept == voice
    assert consistent_vectors(voice) == voice
    assert consistent_vectors(voice[:1]) == []


def test_a_mixed_cluster_cannot_pass_on_a_lucky_pair():
    # Two people split evenly: dropping down to one agreeing pair is not allowed.
    mixed = [[1.0, 0.0], [1.0, 0.05], [0.0, 1.0], [0.05, 1.0], [-1.0, 0.0], [-1.0, 0.05]]
    assert consistent_vectors(mixed) == []


def test_references_enroll_only_the_agreeing_windows():
    from types import SimpleNamespace

    sample = SimpleNamespace(
        id="s" * 64,
        file_id="f1",
        speaker_key="speaker_0",
        source="local",
        reference_name="Gene",
        status="ready",
        vectors=[[1.0, 0.0], [1.0, 0.05], [0.0, 1.0], [1.0, -0.05]],
    )
    (ref,) = references([sample])
    assert ref["vectors"] == [[1.0, 0.0], [1.0, 0.05], [1.0, -0.05]]
    sample.vectors = [[1.0, 0.0], [0.0, 1.0]]
    assert references([sample]) == []


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


def test_local_only_recording_reuses_voiceprint_without_regenerating_notes(database):
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
    assert {s.stage.value for s in stages} == {"index"}
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


def test_permanent_recording_deletion_removes_voice_data(database):
    from sqlalchemy import delete

    from localplaud.voice_identity import VoiceSample

    session, row, speaker = database
    sample = inventory(session, row)[0]
    apply_match(session, sample, decision())
    session.commit()
    session.execute(delete(PlaudFile).where(PlaudFile.id == row.id))
    session.commit()
    assert list(session.scalars(select(VoiceSample))) == []
    assert list(session.scalars(select(VoiceAssignment))) == []
    assert list(session.scalars(select(VoiceEvent))) == []


def test_trash_during_extraction_is_not_renamed(database):
    session, row, speaker = database
    sample = inventory(session, row)[0]
    row.is_trash = True
    session.commit()
    assert apply_match(session, sample, decision()) == "removed"
    assert speaker.display_name is None


def test_frozen_manual_plaud_enrollment_reuses_only_selected_samples(database):
    session, row, _ = database
    cloud = Transcript(
        file_id=row.id,
        source="cloud",
        provider="plaud",
        model="unknown",
        text="import",
        segments=[
            {"speaker": "Alice", "start": 0, "end": 15},
            {"speaker": "Bob", "start": 16, "end": 30},
        ],
    )
    row.transcripts.append(cloud)
    session.commit()
    samples = inventory(session, row, import_plaud=True)
    selected = next(s for s in samples if s.reference_name == "Alice")
    selected.vectors, selected.status = [[1.0, 0.0]] * 2, "ready"
    session.commit()
    snapshot = {
        "version": 1,
        "id": "test-confirmation",
        "confirmed_manual": True,
        "min_recordings": 5,
        "names": {"Alice": 5},
        "samples": {selected.id: "Alice"},
    }
    active = inventory(session, row, import_plaud=True, plaud_enrollment=snapshot)
    assert [s.id for s in active if s.source == "plaud-reference"] == [selected.id]
    refs = references(active, plaud_enrollment=snapshot)
    assert refs[0]["label_provenance"] == "user-confirmed-manual"
    assert refs[0]["enrollment_id"] == "test-confirmation"
    assert refs[0]["vectors"] == [[1.0, 0.0]] * 2
    # New labels/timelines aren't silently added to this one-time snapshot.
    cloud.segments = [{"speaker": "Alice", "start": 40, "end": 60}]
    session.commit()
    assert not [
        s
        for s in inventory(session, row, import_plaud=True, plaud_enrollment=snapshot)
        if s.source == "plaud-reference"
    ]
    # A matching name in a new recording is also excluded, even if a caller
    # mistakenly supplies all cached sample rows directly to references().
    from types import SimpleNamespace

    other = SimpleNamespace(
        id="b" * 64,
        source="plaud-reference",
        status="ready",
        reference_name="Alice",
        vectors=[[1.0, 0.0]] * 2,
    )
    assert references([other], plaud_enrollment=snapshot) == []


def test_frozen_plaud_enrollment_keeps_future_local_manual_names(database):
    session, row, speaker = database
    speaker.display_name = "New manual name"
    sample = inventory(session, row)[0]
    sample.vectors, sample.status = [[1.0, 0.0]] * 2, "ready"
    snapshot = {
        "version": 1,
        "id": "empty",
        "confirmed_manual": True,
        "min_recordings": 5,
        "names": {},
        "samples": {},
    }
    refs = references(inventory(session, row, plaud_enrollment=snapshot), plaud_enrollment=snapshot)
    assert refs[0]["name"] == "New manual name"
    assert refs[0]["label_provenance"] == "local-manual"


def test_invalid_frozen_enrollment_fails_closed(database):
    from localplaud.voice_identity import validate_plaud_enrollment

    with pytest.raises(ValueError):
        validate_plaud_enrollment({})
    with pytest.raises(ValueError):
        validate_plaud_enrollment(
            {
                "version": 1,
                "id": "invalid",
                "confirmed_manual": True,
                "min_recordings": 5,
                "names": {"Alice": 4},
                "samples": {},
            }
        )


def test_explicit_alias_pools_references_without_changing_snapshot_or_vectors():
    from types import SimpleNamespace

    from localplaud.voice_matching import VoiceMatcher

    samples = [
        SimpleNamespace(
            id=sid,
            file_id=fid,
            speaker_key=name,
            source="plaud-reference",
            reference_name=name,
            status="ready",
            vectors=[[1.0, 0.0]] * 2,
        )
        for sid, fid, name in [("a" * 64, "f1", "Alice"), ("b" * 64, "f2", "alice")]
    ]
    snapshot = {
        "version": 1,
        "id": "confirmed",
        "confirmed_manual": True,
        "min_recordings": 5,
        "names": {"Alice": 5, "alice": 5},
        "samples": {s.id: s.reference_name for s in samples},
    }
    before = copy.deepcopy(snapshot)
    assert {r["name"] for r in references(samples, plaud_enrollment=snapshot)} == {"Alice", "alice"}
    refs = references(samples, plaud_enrollment=snapshot, name_aliases={"alice": "Alice"})
    assert {r["name"] for r in refs} == {"Alice"}
    assert {r["source_name"] for r in refs} == {"Alice", "alice"}
    assert len({r["sample_id"] for r in refs}) == 2
    assert snapshot == before
    match = VoiceMatcher(refs).match([[1.0, 0.0]] * 2, query_file_id="new")
    assert (match["status"], match["name"]) == ("matched", "Alice")
    # Two labels in the SAME recording still only supply one recording vote.
    samples[1].file_id = "f1"
    refs = references(samples, plaud_enrollment=snapshot, name_aliases={"alice": "Alice"})
    assert VoiceMatcher(refs).match([[1.0, 0.0]] * 2, query_file_id="new")["status"] == "ambiguous"


@pytest.mark.parametrize(
    "aliases", [{"a": "b", "b": "a"}, {"a": "a"}, {"a": "b", "b": "c"}, {"a": ""}, []]
)
def test_alias_validation_rejects_cycles_chains_and_invalid_names(aliases):
    from localplaud.voice_identity import validate_name_aliases

    with pytest.raises(ValueError):
        validate_name_aliases(aliases)


def test_automatic_name_and_undo_update_notes_with_history_preserving_raw(database):
    from localplaud.db.models import StageName, StageStatus, Summary, SummaryRevision

    session, row, speaker = database
    sample = inventory(session, row)[0]
    raw = copy.deepcopy(row.local_transcript.segments)
    note = Summary(
        file_id=row.id, source="local", template="meeting", content_md="Speaker 1 owns follow-up."
    )
    session.add(note)
    session.add(
        StageRun(
            file_id=row.id,
            stage=StageName.summarize,
            status=StageStatus.completed,
            detail={"stale": False},
        )
    )
    session.commit()
    assert apply_match(session, sample, decision()) == "applied"
    session.commit()
    assert note.content_md == "Alice owns follow-up."
    assert session.scalar(select(SummaryRevision)).content_md == "Speaker 1 owns follow-up."
    assert undo_assignment(session, speaker.id)
    session.commit()
    assert note.content_md == "Speaker 1 owns follow-up."
    assert row.local_transcript.segments == raw
    run = session.scalar(select(StageRun).where(StageRun.stage == StageName.summarize))
    assert run.status == StageStatus.completed and run.detail == {"stale": False}
    assert len(list(session.scalars(select(SummaryRevision)))) == 2


@pytest.mark.parametrize("undo", [False, True])
def test_busy_note_index_preflight_never_partially_changes_identity_or_notes(database, undo):
    from datetime import UTC, datetime, timedelta

    from localplaud.db.models import Summary, SummaryRevision
    from localplaud.worker.knowledge_index import KnowledgeIndexBusyError, sync_summary_document

    session, row, speaker = database
    sample = inventory(session, row)[0]
    notes = [
        Summary(
            file_id=row.id,
            source="local",
            template=template,
            input_transcript_id=row.local_transcript.id,
            input_transcript_revision=0,
            input_transcript_source="local",
            content_md="Speaker 1 owns follow-up.",
        )
        for template in ("first", "busy")
    ]
    session.add_all(notes)
    session.commit()
    if undo:
        assert apply_match(session, sample, decision()) == "applied"
        session.commit()
    before_names = speaker.display_name
    before_notes = [note.content_md for note in notes]
    before_history = len(list(session.scalars(select(SummaryRevision))))
    before_events = len(list(session.scalars(select(VoiceEvent))))
    document = sync_summary_document(session, notes[-1])
    document.status = "running"
    document.lease_token = "active-index"
    document.lease_until = datetime.now(UTC) + timedelta(minutes=5)
    session.commit()
    with pytest.raises(KnowledgeIndexBusyError):
        if undo:
            undo_assignment(session, speaker.id)
        else:
            apply_match(session, sample, decision())
    # Deliberately commit after catching: preflight must itself prevent partial writes.
    session.commit()
    assert speaker.display_name == before_names
    assert [note.content_md for note in notes] == before_notes
    assert len(list(session.scalars(select(SummaryRevision)))) == before_history
    assert len(list(session.scalars(select(VoiceEvent)))) == before_events
    assignment = session.get(VoiceAssignment, speaker.id)
    assert assignment.status == "applied" if undo else assignment is None


def test_uncertain_voice_match_does_not_name_or_rewrite_notes(database):
    from localplaud.db.models import Summary, SummaryRevision

    session, row, speaker = database
    sample = inventory(session, row)[0]
    note = Summary(
        file_id=row.id, source="local", template="meeting", content_md="Speaker 1 owns follow-up."
    )
    session.add(note)
    session.commit()
    unknown = decision() | {"status": "unknown", "name": None}
    assert apply_match(session, sample, unknown) == "unknown"
    session.commit()
    assert speaker.display_name is None
    assert note.content_md == "Speaker 1 owns follow-up."
    assert not list(session.scalars(select(SummaryRevision)))
    assert not list(session.scalars(select(StageRun)))


# --------------------------------------------------------------------------- #
# Calibrated confidence: probability, not raw similarity
# --------------------------------------------------------------------------- #


def _unit(angle_degrees, dims=8, axis=0):
    import math as _math

    angle = _math.radians(angle_degrees)
    vector = [0.0] * dims
    vector[axis], vector[axis + 1] = _math.cos(angle), _math.sin(angle)
    return vector


def _references(name, count, base_angle, noise=2.0):
    return [
        {
            "name": name,
            "file_id": f"{name}-{i}",
            "speaker_key": "s",
            "vectors": [_unit(base_angle + noise * j) for j in range(4)],
            "sample_id": f"{name}-{i}",
        }
        for i in range(count)
    ]


def test_probability_rises_with_similarity_margin_and_independent_recordings():
    from localplaud.voice_matching import calibrated_probability

    base = calibrated_probability(0.70, 0.55, 8)
    assert calibrated_probability(0.80, 0.55, 8) > base
    assert calibrated_probability(0.70, 0.40, 8) > base
    assert calibrated_probability(0.70, 0.55, 20) > base
    assert calibrated_probability(0.70, 0.55, 1) < base
    assert 0.0 <= calibrated_probability(0.2, 0.2, 1) < 0.1
    assert 0.9 < calibrated_probability(0.85, 0.5, 40) <= 1.0
    # Corrupt inputs never become confidence.
    assert calibrated_probability(float("nan"), 0.1, 3) == 0.0


def _axis_refs(name, files, axis, dims=8):
    vector = [0.0] * dims
    vector[axis] = 1.0
    return [
        {
            "name": name,
            "file_id": f"{name}-{i}",
            "speaker_key": "s",
            "vectors": [vector, vector, vector],
            "sample_id": f"{name}-{i}",
        }
        for i in range(files)
    ]


def _query(*components, dims=8):
    """A unit query whose cosine to each axis reference is the given component."""
    import math as _math

    values = list(components) + [0.0] * (dims - len(components))
    norm = _math.sqrt(sum(v * v for v in values))
    return [[v / norm for v in values]] * 4


def test_two_names_almost_equally_close_are_never_named_however_similar():
    from localplaud.voice_matching import MIN_LEAD, VoiceMatcher

    refs = _references("Alice", 6, 0) + _references("Bob", 6, 10)
    near_both = [_unit(5, axis=0) for _ in range(4)]
    result = VoiceMatcher(refs).match(near_both, policy="calibrated", min_probability=0.3)
    assert result["score"] - result["runner_up"] < MIN_LEAD
    assert result["status"] == "ambiguous"


def test_calibrated_policy_names_a_clear_lead_the_strict_rule_would_leave_ambiguous():
    from localplaud.voice_matching import VoiceMatcher

    # Alice is clearly closest (0.999) with a runner-up at 0.92: a lead of 0.08, below
    # the strict rule's 0.12 margin but plenty for the calibrated probability.
    refs = _references("Alice", 8, 0) + _references("Bob", 8, 25)
    matcher = VoiceMatcher(refs)
    query = [_unit(2) for _ in range(4)]
    strict = matcher.match(query, threshold=0.75, margin=0.12)
    calibrated = matcher.match(query, policy="calibrated", min_probability=0.7)
    assert strict["status"] == "ambiguous"
    assert calibrated["status"] == "matched" and calibrated["name"] == "Alice"
    assert calibrated["probability"] >= 0.7 and calibrated["files"] == 8
    assert calibrated["policy"] == "calibrated"
    assert calibrated["calibration"] == "voice-calibration/v1"


def test_calibrated_threshold_is_the_probability_the_user_chose():
    from localplaud.voice_matching import VoiceMatcher

    # Alice 0.66 vs Bob 0.53 with three recordings each: about a one in three chance.
    refs = _axis_refs("Alice", 3, 0) + _axis_refs("Bob", 3, 1)
    query = _query(0.66, 0.53, 0.53)
    matcher = VoiceMatcher(refs)
    loose = matcher.match(query, policy="calibrated", min_probability=0.3)
    tight = matcher.match(query, policy="calibrated", min_probability=0.99)
    assert loose["probability"] == tight["probability"]
    assert 0.3 <= loose["probability"] < 0.5
    assert loose["status"] == "matched" and loose["name"] == "Alice"
    assert tight["status"] == "ambiguous"


def test_similarity_floor_blocks_naming_whatever_the_model_says():
    from localplaud.voice_matching import VoiceMatcher

    refs = _references("Alice", 40, 0)
    far = [_unit(75) for _ in range(4)]  # similarity ~0.26
    result = VoiceMatcher(refs).match(far, policy="calibrated", min_probability=0.3)
    assert result["status"] == "unknown"


def test_unknown_policy_is_rejected():
    from localplaud.voice_matching import VoiceMatcher

    with pytest.raises(ValueError, match="policy"):
        VoiceMatcher(_references("Alice", 3, 0)).match([_unit(0)] * 3, policy="reckless")


def _matched(name, probability):
    return {"status": "matched", "name": name, "score": 0.7, "probability": probability}


def test_one_name_per_recording_keeps_only_the_most_probable_speaker():
    from localplaud.voice_matching import resolve_name_conflicts

    decisions = {
        "s1": _matched("Sky", 0.95),
        "s2": _matched("Sky", 0.72),
        "s3": _matched("Grace", 0.8),
        "s4": {"status": "unknown", "name": "Sky", "score": 0.4},
    }
    resolve_name_conflicts(decisions)
    assert decisions["s1"]["status"] == "matched"
    assert decisions["s2"]["status"] == "ambiguous"
    assert decisions["s2"]["demoted"] == "name_taken_in_recording"
    assert decisions["s2"]["name"] == "Sky"  # still available as a suggestion
    assert decisions["s3"]["status"] == "matched"
    assert decisions["s4"]["status"] == "unknown" and "demoted" not in decisions["s4"]


def test_a_name_already_held_by_another_speaker_is_unavailable():
    from localplaud.voice_matching import resolve_name_conflicts

    decisions = {"s1": _matched("Sky", 0.99), "s2": _matched("Sky", 0.9)}
    resolve_name_conflicts(decisions, taken={"s9": "Sky"})  # a person named s9 Sky
    assert {d["status"] for d in decisions.values()} == {"ambiguous"}

    # The holder itself keeps the name when the voice agrees.
    decisions = {"s1": _matched("Sky", 0.6), "s2": _matched("Sky", 0.99)}
    resolve_name_conflicts(decisions, taken={"s1": "Sky"})
    assert decisions["s1"]["status"] == "matched"
    assert decisions["s2"]["status"] == "ambiguous"


@pytest.mark.parametrize(
    ("profile", "ok"),
    [
        ({}, True),
        ({"policy": "calibrated", "min_probability": 0.7}, True),
        ({"policy": "calibrated", "min_probability": 0.5}, True),
        ({"policy": "calibrated", "min_probability": 0.3}, True),
        ({"policy": "calibrated", "min_probability": 0.2}, False),
        ({"policy": "calibrated", "min_probability": 1.0}, False),
        ({"policy": "calibrated", "min_probability": True}, False),
        ({"policy": "calibrated", "min_probability": "0.7"}, False),
        ({"policy": "yolo"}, False),
        ({"threshold": 0.4}, False),
    ],
)
def test_profile_limits_refuse_reckless_matching(profile, ok):
    from localplaud.voice_service import validate_profile_thresholds

    if ok:
        validate_profile_thresholds(profile)
    else:
        with pytest.raises(ValueError):
            validate_profile_thresholds(profile)


def test_calibrated_name_is_applied_marked_automatic_and_a_person_can_override_it(database):
    from localplaud.voice_suggestions import automatic_names

    session, row, speaker = database
    sample = inventory(session, row)[0]
    calibrated = {
        **decision(),
        "policy": "calibrated",
        "probability": 0.74,
        "calibration": "voice-calibration/v1",
        "min_probability": 0.7,
    }
    assert apply_match(session, sample, calibrated) == "applied"
    session.commit()
    assignment = session.get(VoiceAssignment, speaker.id)
    assert assignment.status == "applied" and assignment.evidence["probability"] == 0.74
    assert automatic_names(session, row.id) == {
        "s": {"name": "Alice", "probability": 0.74, "score": 0.9}
    }

    # A person renames the speaker: it is theirs now, no longer automatic, and a later
    # scan never puts the voice-matched name back.
    speaker.display_name = "Alicia"
    session.commit()
    assert automatic_names(session, row.id) == {}
    assert apply_match(session, inventory(session, row)[0], calibrated) == "manual"
    assert speaker.display_name == "Alicia"
