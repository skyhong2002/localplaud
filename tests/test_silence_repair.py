"""Operator-only acoustic transcript repair stays geometric and non-destructive."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select


@pytest.fixture
def repair_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.models import Base

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'repair.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    settings = get_settings(reload=True)
    Base.metadata.create_all(db_session.get_engine())
    yield settings, tmp_path
    db_session.get_engine().dispose()
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)


def _segments():
    return [
        {
            "text": "李宗盛談音樂",
            "start": 10.0,
            "end": 12.0,
            "speaker": "SPEAKER_00",
            "words": [
                {
                    "text": "李宗盛",
                    "start": 10.1,
                    "end": 10.8,
                    "speaker": "SPEAKER_00",
                    "confidence": 0.98,
                }
            ],
        },
        {
            "text": "completely arbitrary hallucination",
            "start": 30.0,
            "end": 31.0,
            "speaker": "SPEAKER_00",
            "words": [{"text": "unchanged", "start": 30.0, "end": 30.2}],
        },
    ]


def _seed(
    tmp_path,
    *,
    file_id="recording",
    segments=None,
    revision_kind=None,
    with_derived=False,
):
    from localplaud.db.models import (
        Chunk,
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
        TranscriptRevision,
    )
    from localplaud.db.session import session_scope

    audio = tmp_path / f"{file_id}.wav"
    audio.write_bytes(b"RIFF stable operator test audio")
    source_segments = copy.deepcopy(_segments() if segments is None else segments)
    with session_scope() as session:
        row = PlaudFile(
            id=file_id,
            filename="Plaud source name",
            local_title="My local title",
            generated_title="Generated subject",
            generated_title_provider="ollama",
            generated_title_model="local-model",
            generated_title_at=datetime(2026, 9, 1, tzinfo=UTC),
            status=FileStatus.done,
            origin="local",
            audio_path=str(audio),
            wav_path=str(audio),
        )
        raw = Transcript(
            file_id=file_id,
            provider="faster-whisper",
            model="large-v3-turbo",
            source="local",
            text="\n".join(str(item.get("text") or "") for item in source_segments),
            segments=copy.deepcopy(source_segments),
            has_speakers=True,
        )
        row.transcripts.append(raw)
        session.add(row)
        session.flush()
        if revision_kind is not None:
            row.transcript_revisions.append(
                TranscriptRevision(
                    file_id=file_id,
                    base_transcript_id=raw.id,
                    revision=1,
                    source="local",
                    text=raw.text,
                    segments=copy.deepcopy(source_segments),
                    has_speakers=True,
                    kind=revision_kind,
                )
            )
        if with_derived:
            session.add(
                Chunk(
                    file_id=file_id,
                    idx=0,
                    text="old searchable transcript",
                    input_transcript_id=raw.id,
                    input_transcript_revision=1 if revision_kind else None,
                    input_transcript_source="local",
                )
            )
            for stage in (StageName.summarize, StageName.mind_map, StageName.index):
                session.add(
                    StageRun(
                        file_id=file_id,
                        stage=stage,
                        status=StageStatus.completed,
                        attempts=1,
                        detail={"old": True},
                        completed_at=datetime.now(UTC),
                    )
                )
    return audio, source_segments


def _plan(monkeypatch, settings, *, regions=((10.2, 11.0),), file_id="recording"):
    import localplaud.silence_repair as repair

    monkeypatch.setattr(repair, "detect_speech", lambda _path, _cfg: list(regions))
    return repair.plan_repair(file_id, settings)


def _profile_snapshot():
    return {
        "schema": "localplaud-resolved-profile/v2",
        "policy": {"no_egress": True},
        "stages": {
            "transcribe": {
                "connection": "worker:gpu",
                "model": "large-v3-turbo",
                "provider_type": "remote-worker",
                "execution_target": "remote_worker",
                "data_egress": False,
                "configuration": {},
                "secret_ref": "env:LOCALPLAUD_GPU_WORKER_KEY",
            },
            "diarize": {
                "connection": "worker:gpu",
                "model": "pyannote/speaker-diarization-community-1",
                "provider_type": "remote-worker",
                "execution_target": "remote_worker",
                "data_egress": False,
                "configuration": {},
                "secret_ref": "env:LOCALPLAUD_GPU_WORKER_KEY",
            },
        },
        "layers": ["gpu-reviewed"],
        "layer_provenance": [{"kind": "recording", "key": "gpu-reviewed"}],
    }


def _replacement_segments():
    return [
        {
            "text": "李宗盛正在說真實的訪談內容",
            "start": 10.0,
            "end": 12.0,
            "speaker": "SPEAKER_00",
            "words": [
                {
                    "text": "李宗盛",
                    "start": 10.1,
                    "end": 10.8,
                    "speaker": "SPEAKER_00",
                }
            ],
        },
        {
            "text": "這是重新辨識出的第二段對話",
            "start": 30.0,
            "end": 31.0,
            "speaker": "SPEAKER_01",
            "words": [],
        },
    ]


def _apply_retranscription(plan, replacement, settings, *, profile=None):
    from localplaud.silence_repair import apply_retranscription

    return apply_retranscription(
        plan,
        {"segments": replacement, "has_speakers": True},
        provider="faster-whisper",
        model="large-v3-turbo",
        profile_snapshot=profile or _profile_snapshot(),
        settings=settings,
    )


def test_real_name_in_speech_is_retained_and_arbitrary_text_outside_removed(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.silence_repair import apply_repair

    settings, tmp_path = repair_db
    _audio, original = _seed(tmp_path)
    plan = _plan(monkeypatch, settings)

    assert plan["removed_segment_indices"] == [1]
    result = apply_repair(plan, settings)
    assert result == {
        "file_id": "recording",
        "status": "applied",
        "segments_before": 2,
        "segments_removed": 1,
        "segments_after": 1,
        "revision": 1,
    }
    with session_scope() as session:
        revision = session.get(PlaudFile, "recording").corrected_transcript
        assert revision.segments == [original[0]]
        assert revision.segments[0]["words"] == original[0]["words"]
        assert revision.text == "李宗盛談音樂"


def test_invalid_or_unknown_timestamps_are_always_retained(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.silence_repair import apply_repair

    settings, tmp_path = repair_db
    invalid = [
        {"text": "missing"},
        {"text": "none", "start": None, "end": 2.0},
        {"text": "reversed", "start": 4.0, "end": 3.0},
        {"text": "negative", "start": -1.0, "end": 1.0},
    ]
    _seed(tmp_path, segments=invalid)
    plan = _plan(monkeypatch, settings, regions=())
    assert plan["removed_segment_indices"] == []
    assert apply_repair(plan, settings)["status"] == "no_change"
    with session_scope() as session:
        assert session.get(PlaudFile, "recording").transcript_revisions == []


def test_empty_canonical_revision_is_allowed(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.silence_repair import apply_repair

    settings, tmp_path = repair_db
    _seed(tmp_path, segments=[{"text": "noise", "start": 20.0, "end": 21.0, "words": []}])
    result = apply_repair(_plan(monkeypatch, settings, regions=()), settings)
    assert result["status"] == "applied" and result["segments_after"] == 0
    with session_scope() as session:
        revision = session.get(PlaudFile, "recording").corrected_transcript
        assert revision.segments == []
        assert revision.text == ""


@pytest.mark.parametrize("kind", ["user_edit", "restore", "speaker_edit"])
def test_human_revision_kinds_skip_repair(monkeypatch, repair_db, kind):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.silence_repair import apply_repair

    settings, tmp_path = repair_db
    _seed(tmp_path, revision_kind=kind)
    result = apply_repair(_plan(monkeypatch, settings), settings)
    assert result["status"] == "skipped_user_edits"
    with session_scope() as session:
        revisions = session.get(PlaudFile, "recording").transcript_revisions
        assert len(revisions) == 1 and revisions[0].kind == kind


def test_stale_plan_is_rejected_without_mutation(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.silence_repair import apply_repair

    settings, tmp_path = repair_db
    _seed(tmp_path)
    plan = _plan(monkeypatch, settings)
    with session_scope() as session:
        raw = session.get(PlaudFile, "recording").local_transcript
        raw.segments = [dict(raw.segments[0]) | {"end": 12.1}, raw.segments[1]]
    result = apply_repair(plan, settings)
    assert result["status"] == "stale"
    with session_scope() as session:
        assert session.get(PlaudFile, "recording").transcript_revisions == []


def test_apply_is_idempotent(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.silence_repair import apply_repair

    settings, tmp_path = repair_db
    _seed(tmp_path)
    plan = _plan(monkeypatch, settings)
    assert apply_repair(plan, settings)["status"] == "applied"
    assert apply_repair(plan, settings)["status"] == "stale"
    with session_scope() as session:
        assert len(session.get(PlaudFile, "recording").transcript_revisions) == 1


def test_changed_audio_hash_rejects_plan(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.silence_repair import apply_repair

    settings, tmp_path = repair_db
    audio, _segments_before = _seed(tmp_path)
    plan = _plan(monkeypatch, settings)
    audio.write_bytes(b"RIFF replaced audio")
    assert apply_repair(plan, settings)["status"] == "stale"
    with session_scope() as session:
        assert session.get(PlaudFile, "recording").transcript_revisions == []


def test_active_processing_lease_blocks_apply(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.silence_repair import SilenceRepairError, apply_repair

    settings, tmp_path = repair_db
    _seed(tmp_path)
    plan = _plan(monkeypatch, settings)
    with session_scope() as session:
        row = session.get(PlaudFile, "recording")
        row.processing_token = "worker"
        row.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)
    with pytest.raises(SilenceRepairError, match="currently processing"):
        apply_repair(plan, settings)
    with session_scope() as session:
        assert session.get(PlaudFile, "recording").transcript_revisions == []


def test_active_ask_lease_blocks_apply(monkeypatch, repair_db):
    from localplaud.db.models import AskThread, PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.silence_repair import apply_repair
    from localplaud.worker.knowledge_index import KnowledgeIndexBusyError

    settings, tmp_path = repair_db
    _seed(tmp_path)
    plan = _plan(monkeypatch, settings)
    with session_scope() as session:
        session.add(
            AskThread(
                id="active-ask",
                file_id="recording",
                title="active",
                request_token="request",
                request_lease_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
    with pytest.raises(KnowledgeIndexBusyError, match="currently being used by Ask"):
        apply_repair(plan, settings)
    with session_scope() as session:
        assert session.get(PlaudFile, "recording").transcript_revisions == []


def test_raw_and_old_revision_survive_while_title_and_index_are_invalidated(monkeypatch, repair_db):
    from localplaud.db.models import Chunk, PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.silence_repair import apply_repair

    settings, tmp_path = repair_db
    _audio, original = _seed(tmp_path, revision_kind="vocabulary", with_derived=True)
    result = apply_repair(_plan(monkeypatch, settings), settings)
    assert result["status"] == "applied" and result["revision"] == 2

    with session_scope() as session:
        row = session.get(PlaudFile, "recording")
        assert row.local_transcript.segments == original
        assert [item.kind for item in row.transcript_revisions] == [
            "vocabulary",
            "speech_cleanup",
        ]
        cleanup = row.transcript_revisions[-1]
        assert cleanup.provider == "silero-vad"
        assert cleanup.prompt_version == "speech-cleanup/v1"
        assert json.loads(cleanup.note)["removed_indices"] == [1]
        assert row.generated_title is None
        assert row.generated_title_provider is None
        assert row.generated_title_model is None
        assert row.generated_title_at is None
        assert row.local_title == "My local title"
        assert session.scalars(select(Chunk).where(Chunk.file_id == row.id)).all() == []
        runs = {
            run.stage: run
            for run in session.scalars(select(StageRun).where(StageRun.file_id == row.id))
        }
        for stage in (StageName.summarize, StageName.mind_map, StageName.index):
            assert runs[stage].status == StageStatus.pending
            assert runs[stage].detail["stale"] is True
        assert runs[StageName.index].detail["reindex_only"] is True
        assert runs[StageName.index].detail["reason"] == "canonical transcript changed"


def test_plan_is_json_serializable_and_contains_no_transcript_text(monkeypatch, repair_db):
    settings, tmp_path = repair_db
    _seed(tmp_path)
    plan = _plan(monkeypatch, settings)
    encoded = json.dumps(plan, ensure_ascii=False)
    assert "李宗盛" not in encoded
    assert "arbitrary hallucination" not in encoded
    assert plan["vad_config"]["threshold"] == 0.25
    assert plan["vad_config"]["min_speech_ms"] == 100
    assert plan["vad_config"]["speech_pad_ms"] == 300


def test_retranscription_replaces_when_acoustic_removal_count_is_zero_and_preserves_history(
    monkeypatch, repair_db
):
    from localplaud.db.models import Chunk, PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope

    settings, tmp_path = repair_db
    _audio, original = _seed(
        tmp_path,
        revision_kind="vocabulary",
        with_derived=True,
    )
    plan = _plan(monkeypatch, settings, regions=((9.0, 32.0),))
    assert plan["removed_segment_indices"] == []
    replacement = _replacement_segments()
    result = _apply_retranscription(plan, replacement, settings)
    assert result == {
        "file_id": "recording",
        "status": "applied",
        "segments_before": 2,
        "segments_after": 2,
        "segments_changed": 2,
        "revision": 2,
    }

    with session_scope() as session:
        row = session.get(PlaudFile, "recording")
        assert row.local_transcript.segments == original
        assert [revision.kind for revision in row.transcript_revisions] == [
            "vocabulary",
            "speech_retranscribe",
        ]
        retranscribed = row.transcript_revisions[-1]
        assert retranscribed.segments == replacement
        assert retranscribed.text == "\n".join(item["text"] for item in replacement)
        assert retranscribed.has_speakers is True
        assert retranscribed.provider == "faster-whisper"
        assert retranscribed.model == "large-v3-turbo"
        assert retranscribed.prompt_version == "speech-retranscribe/v1"
        assert retranscribed.resolved_profile_snapshot == _profile_snapshot()
        audit = json.loads(retranscribed.note)
        assert audit["method"] == "full_retranscription"
        assert audit["segments_before"] == 2
        assert audit["segments_after"] == 2
        assert audit["segments_changed"] == 2
        assert "plan" in audit
        assert "old_title" in audit
        assert len(retranscribed.note) <= 256
        assert replacement[0]["text"] not in retranscribed.note
        assert replacement[1]["text"] not in retranscribed.note
        assert row.generated_title is None
        assert row.generated_title_provider is None
        assert row.generated_title_model is None
        assert row.generated_title_at is None
        assert row.local_title == "My local title"
        assert session.scalars(select(Chunk).where(Chunk.file_id == row.id)).all() == []
        runs = {
            run.stage: run
            for run in session.scalars(select(StageRun).where(StageRun.file_id == row.id))
        }
        for stage in (StageName.summarize, StageName.mind_map, StageName.index):
            assert runs[stage].status == StageStatus.pending
            assert runs[stage].detail["stale"] is True
        assert runs[StageName.index].detail["reindex_only"] is True
        assert runs[StageName.index].detail["reason"] == "canonical transcript changed"


def test_retranscription_accepts_empty_result(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    settings, tmp_path = repair_db
    _seed(tmp_path)
    plan = _plan(monkeypatch, settings, regions=((9.0, 32.0),))
    result = _apply_retranscription(plan, [], settings)
    assert result["status"] == "applied"
    assert result["segments_after"] == 0
    with session_scope() as session:
        revision = session.get(PlaudFile, "recording").corrected_transcript
        assert revision.kind == "speech_retranscribe"
        assert revision.segments == []
        assert revision.text == ""


def test_retranscription_stale_and_human_guards_preserve_canonical(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    settings, tmp_path = repair_db
    _seed(tmp_path)
    plan = _plan(monkeypatch, settings, regions=((9.0, 32.0),))
    with session_scope() as session:
        raw = session.get(PlaudFile, "recording").local_transcript
        raw.segments = [dict(raw.segments[0]) | {"end": 12.2}, raw.segments[1]]
    assert _apply_retranscription(plan, _replacement_segments(), settings)["status"] == "stale"
    with session_scope() as session:
        assert session.get(PlaudFile, "recording").transcript_revisions == []


def test_retranscription_skips_any_human_revision(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    settings, tmp_path = repair_db
    _seed(tmp_path, revision_kind="speaker_edit")
    plan = _plan(monkeypatch, settings, regions=((9.0, 32.0),))
    result = _apply_retranscription(plan, _replacement_segments(), settings)
    assert result["status"] == "skipped_user_edits"
    with session_scope() as session:
        revisions = session.get(PlaudFile, "recording").transcript_revisions
        assert len(revisions) == 1 and revisions[0].kind == "speaker_edit"


@pytest.mark.parametrize(
    "segments, error",
    [
        ([{"text": "bad", "start": -1.0, "end": 1.0}], "nonnegative"),
        ([{"text": "bad", "start": "0.0", "end": 1.0}], "number"),
        ([{"text": "bad", "start": 2.0, "end": 1.0}], "after start"),
        ([{"text": "bad", "start": 0.0, "end": float("inf")}], "finite"),
        (
            [
                {"text": "later", "start": 3.0, "end": 4.0},
                {"text": "earlier", "start": 1.0, "end": 2.0},
            ],
            "ordered",
        ),
        (
            [
                {
                    "text": "bad word",
                    "start": 1.0,
                    "end": 2.0,
                    "words": [{"text": "outside", "start": 0.5, "end": 1.5}],
                }
            ],
            "bounds",
        ),
        ([{"text": "bad words", "start": 1.0, "end": 2.0, "words": None}], "list"),
    ],
)
def test_retranscription_rejects_malformed_timestamps(monkeypatch, repair_db, segments, error):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    settings, tmp_path = repair_db
    _seed(tmp_path)
    plan = _plan(monkeypatch, settings, regions=((9.0, 32.0),))
    with pytest.raises(ValueError, match=error):
        _apply_retranscription(plan, segments, settings)
    with session_scope() as session:
        assert session.get(PlaudFile, "recording").transcript_revisions == []


def test_retranscription_rejects_raw_credentials_in_profile(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    settings, tmp_path = repair_db
    _seed(tmp_path)
    plan = _plan(monkeypatch, settings, regions=((9.0, 32.0),))
    profile = _profile_snapshot()
    profile["stages"]["transcribe"]["api_key"] = "must-not-persist"
    with pytest.raises(ValueError, match="raw credentials"):
        _apply_retranscription(plan, _replacement_segments(), settings, profile=profile)
    with session_scope() as session:
        assert session.get(PlaudFile, "recording").transcript_revisions == []


def test_retranscription_requires_provider_model_and_stage_scoped_profile(monkeypatch, repair_db):
    from localplaud.silence_repair import apply_retranscription

    settings, tmp_path = repair_db
    _seed(tmp_path)
    plan = _plan(monkeypatch, settings, regions=((9.0, 32.0),))
    payload = {"segments": _replacement_segments(), "has_speakers": True}
    with pytest.raises(ValueError, match="provider"):
        apply_retranscription(
            plan,
            payload,
            provider="",
            model="large-v3-turbo",
            profile_snapshot=_profile_snapshot(),
            settings=settings,
        )
    profile = _profile_snapshot()
    del profile["stages"]["diarize"]
    with pytest.raises(ValueError, match="diarize"):
        apply_retranscription(
            plan,
            payload,
            provider="faster-whisper",
            model="large-v3-turbo",
            profile_snapshot=profile,
            settings=settings,
        )


def test_retranscription_reapplication_is_stale_then_new_plan_is_no_change(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    settings, tmp_path = repair_db
    _seed(tmp_path)
    replacement = _replacement_segments()
    first_plan = _plan(monkeypatch, settings, regions=((9.0, 32.0),))
    assert _apply_retranscription(first_plan, replacement, settings)["status"] == "applied"
    assert _apply_retranscription(first_plan, replacement, settings)["status"] == "stale"
    current_plan = _plan(monkeypatch, settings, regions=((9.0, 32.0),))
    unchanged = _apply_retranscription(current_plan, replacement, settings)
    assert unchanged["status"] == "no_change"
    assert unchanged["segments_changed"] == 0
    with session_scope() as session:
        revisions = session.get(PlaudFile, "recording").transcript_revisions
        assert len(revisions) == 1 and revisions[0].kind == "speech_retranscribe"


def test_retranscription_preserves_quantized_zero_duration_words(monkeypatch, repair_db):
    settings, tmp_path = repair_db
    _seed(tmp_path)
    plan = _plan(monkeypatch, settings, regions=((9.0, 32.0),))
    segments = [{"text": "李宗盛", "start": 10.0, "end": 11.0,
                 "words": [{"text": "李宗盛", "start": 10.0, "end": 10.0}]}]
    result = _apply_retranscription(plan, segments, settings)
    assert result["status"] == "applied"


def test_retranscription_preserves_manual_vocabulary(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    settings, tmp_path = repair_db
    _seed(tmp_path, revision_kind='vocabulary')
    with session_scope() as session:
        session.get(PlaudFile, 'recording').corrected_transcript.note = 'vocabulary:manual rules=1'
    plan = _plan(monkeypatch, settings)
    assert _apply_retranscription(plan, _replacement_segments(), settings)['status'] == 'skipped_user_edits'


def test_retranscription_does_not_reassign_human_speaker_names(monkeypatch, repair_db):
    from localplaud.db.models import Speaker
    from localplaud.db.session import session_scope
    settings, tmp_path = repair_db
    _seed(tmp_path)
    with session_scope() as session:
        session.add(Speaker(file_id='recording', key='SPEAKER_00', display_name='Named speaker'))
    plan = _plan(monkeypatch, settings)
    assert _apply_retranscription(plan, _replacement_segments(), settings)['status'] == 'skipped_user_edits'


def test_retranscription_updates_speaker_completeness_even_if_text_unchanged(monkeypatch, repair_db):
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.silence_repair import apply_retranscription
    settings, tmp_path = repair_db
    _, segments = _seed(tmp_path)
    plan = _plan(monkeypatch, settings)
    result = apply_retranscription(plan, {'segments': segments, 'has_speakers': False},
        provider='faster-whisper', model='large-v3-turbo', profile_snapshot=_profile_snapshot(),
        settings=settings)
    assert result['status'] == 'applied'
    with session_scope() as session:
        assert session.get(PlaudFile, 'recording').corrected_transcript.has_speakers is False
