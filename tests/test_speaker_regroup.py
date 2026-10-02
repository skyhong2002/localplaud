"""Stored transcripts regroup into clause-level speaker turns without ASR."""

from __future__ import annotations

import copy
import json

import pytest
from sqlalchemy import select

A, B = "SPEAKER_00", "SPEAKER_01"


@pytest.fixture
def regroup_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.models import Base

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'regroup.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    settings = get_settings(reload=True)
    Base.metadata.create_all(db_session.get_engine())
    yield settings
    db_session.get_engine().dispose()
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)


def _window(text, speakers, start=0.0):
    """One Qwen-style ASR window: aligner words carry no punctuation."""
    letters = [char for char in text if char.isalnum()]
    words = [
        {
            "text": char,
            "start": start + index * 0.5,
            "end": start + index * 0.5 + 0.4,
            "speaker": speaker,
            "confidence": None,
        }
        for index, (char, speaker) in enumerate(zip(letters, speakers, strict=True))
    ]
    return {
        "text": text,
        "start": start,
        "end": words[-1]["end"],
        "speaker": max(set(speakers), key=speakers.count),
        "words": words,
    }


RAW = [_window("我们先看这个。好，可以。", [A] * 6 + [B] * 3)]


def _polished(segments):
    polished = copy.deepcopy(segments)
    polished[0]["text"] = "我們先看這一個。好的，可以。"
    for word in polished[0]["words"]:
        word["text"] = {"们": "們", "这": "這", "个": "個"}.get(word["text"], word["text"])
    return polished


def _seed(*, revisions=(), file_id="recording"):
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
        TranscriptRevision,
    )
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import raw_transcript_fingerprint

    with session_scope() as session:
        row = PlaudFile(id=file_id, filename="Recording", status=FileStatus.done, origin="local")
        raw = Transcript(
            file_id=file_id,
            provider="remote-worker",
            model="Qwen/Qwen3-ASR-1.7B-hf",
            source="local",
            text=RAW[0]["text"],
            segments=copy.deepcopy(RAW),
            has_speakers=True,
        )
        row.transcripts.append(raw)
        session.add(row)
        session.flush()
        for number, (kind, segments) in enumerate(revisions, start=1):
            row.transcript_revisions.append(
                TranscriptRevision(
                    file_id=file_id,
                    base_transcript_id=raw.id,
                    revision=number,
                    source="local",
                    text="\n".join(segment["text"] for segment in segments),
                    segments=segments,
                    has_speakers=True,
                    kind=kind,
                    provider="codex-local",
                    model="gpt",
                    prompt_version="transcript-polish/v5",
                    resolved_profile_snapshot={
                        "stages": {"correct": {"model": "gpt"}},
                        "correction_input": {
                            "revision": number - 1 or None,
                            "kind": revisions[number - 2][0] if number > 1 else None,
                            "raw_sha256": raw_transcript_fingerprint(raw),
                        },
                    },
                )
            )
        for stage in (StageName.diarize, StageName.summarize, StageName.index):
            session.add(
                StageRun(file_id=file_id, stage=stage, status=StageStatus.completed, attempts=1,
                         detail={})
            )


def test_apply_regroups_raw_and_carries_polished_wording(regroup_db):
    from localplaud.db.models import PlaudFile, StageName, Transcript, TranscriptRevision
    from localplaud.db.session import session_scope
    from localplaud.speaker_regroup import apply_regroup
    from localplaud.worker.pipeline import raw_transcript_fingerprint

    _seed(revisions=[("ai_polish", _polished(RAW))])

    result = apply_regroup("recording")

    assert result["status"] == "applied"
    assert (result["segments_before"], result["segments_after"]) == (1, 2)
    with session_scope() as session:
        raw = session.scalar(select(Transcript))
        assert [(s["speaker"], s["text"]) for s in raw.segments] == [
            (A, "我们先看这个。"),
            (B, "好，可以。"),
        ]
        assert raw.text == RAW[0]["text"]
        latest = session.scalars(
            select(TranscriptRevision).order_by(TranscriptRevision.revision)
        ).all()[-1]
        assert latest.revision == 2
        assert latest.kind == "ai_polish"
        assert latest.prompt_version == "transcript-polish/v5"
        assert [(s["speaker"], s["text"]) for s in latest.segments] == [
            (A, "我們先看這一個。"),
            (B, "好的，可以。"),
        ]
        assert latest.text == "我們先看這一個。\n好的，可以。"
        snapshot = latest.resolved_profile_snapshot
        # Accepted corrections stay reusable against the regrouped raw lane.
        assert snapshot["correction_input"]["raw_sha256"] == raw_transcript_fingerprint(raw)
        assert snapshot["speaker_regroup"]["from_revision"] == 1
        assert snapshot["stages"] == {"correct": {"model": "gpt"}}
        row = session.get(PlaudFile, "recording")
        runs = {run.stage: run for run in row.stage_runs}
        assert runs[StageName.summarize].detail["stale"] is True
        assert runs[StageName.summarize].detail["reason"] == "speaker_regroup"
        assert runs[StageName.diarize].detail["speaker_grouping"]["output_segments"] == 2


def test_apply_is_idempotent(regroup_db):
    from localplaud.speaker_regroup import apply_regroup

    _seed(revisions=[("ai_polish", _polished(RAW))])

    assert apply_regroup("recording")["status"] == "applied"
    assert apply_regroup("recording")["status"] == "already_current"


def test_apply_regroups_acoustic_base_beneath_polish(regroup_db):
    from localplaud.db.models import TranscriptRevision
    from localplaud.db.session import session_scope
    from localplaud.speaker_regroup import apply_regroup

    _seed(revisions=[("speech_cleanup", copy.deepcopy(RAW)), ("ai_polish", _polished(RAW))])

    assert apply_regroup("recording")["status"] == "applied"
    with session_scope() as session:
        revisions = session.scalars(
            select(TranscriptRevision).order_by(TranscriptRevision.revision)
        ).all()
        assert [(rev.revision, rev.kind) for rev in revisions] == [
            (1, "speech_cleanup"),
            (2, "ai_polish"),
            (3, "speech_cleanup"),
            (4, "ai_polish"),
        ]
        assert len(revisions[2].segments) == 2
        assert revisions[3].resolved_profile_snapshot["correction_input"]["revision"] == 3


def test_apply_never_touches_human_edits(regroup_db):
    from localplaud.db.models import Transcript, TranscriptRevision
    from localplaud.db.session import session_scope
    from localplaud.speaker_regroup import apply_regroup

    _seed(revisions=[("user_edit", copy.deepcopy(RAW))])

    assert apply_regroup("recording")["status"] == "skipped_user_edits"
    with session_scope() as session:
        assert session.scalar(select(Transcript)).segments == RAW
        assert len(session.scalars(select(TranscriptRevision)).all()) == 1


def test_scan_reports_reattributed_speech_without_writing(regroup_db):
    from localplaud.db.models import Transcript
    from localplaud.db.session import session_scope
    from localplaud.speaker_regroup import scan

    _seed()

    [item] = scan()

    assert item["status"] == "changed"
    # Three words (0.4 s each) move from the window's majority speaker to B.
    assert item["reattributed_seconds"] == pytest.approx(1.2)
    with session_scope() as session:
        assert session.scalar(select(Transcript)).segments == RAW


def test_cli_apply_writes_newest_first_recovery_manifest(regroup_db, tmp_path):
    from localplaud.speaker_regroup import main

    _seed()
    manifest = tmp_path / "regroup.json"

    assert main(["apply", "--manifest", str(manifest)]) == 0

    assert json.loads(manifest.read_text()) == {
        "recordings": [{"id": "recording", "outcome": "pending"}]
    }


def test_correction_stage_reuses_regrouped_polish(regroup_db, monkeypatch):
    from localplaud.db.models import PlaudFile, StageName, StageRun
    from localplaud.db.session import session_scope
    from localplaud.speaker_regroup import apply_regroup
    from localplaud.worker.pipeline import _load_transcript, _run_correction_stage

    _seed(revisions=[("ai_polish", _polished(RAW))])
    assert apply_regroup("recording")["status"] == "applied"

    def recharge(*_args, **_kwargs):
        raise AssertionError("accepted corrections must not be recomputed")

    monkeypatch.setattr("localplaud.worker.pipeline.polish.polish_transcript", recharge)
    monkeypatch.setattr(
        "localplaud.worker.pipeline._assert_processing_claim_in_session",
        lambda session, file_id: session.get(PlaudFile, file_id),
    )

    errors = _run_correction_stage(
        "recording", regroup_db, {"stages": {"correct": {"model": "gpt"}}}
    )

    assert errors == []
    with session_scope() as session:
        run = session.scalar(select(StageRun).where(StageRun.stage == StageName.correct))
        assert run.detail["reused"] is True
    transcript, _source = _load_transcript("recording", regroup_db)
    assert [segment.text for segment in transcript.segments] == [
        "我們先看這一個。",
        "好的，可以。",
    ]


def test_polish_over_retranscription_stays_canonical_and_reusable(regroup_db):
    from localplaud.db.models import Transcript, TranscriptRevision
    from localplaud.db.session import session_scope
    from localplaud.speaker_regroup import apply_regroup
    from localplaud.worker.pipeline import _load_transcript, raw_transcript_fingerprint

    # Re-transcription has its own timings, unrelated to the raw lane's words.
    retranscribed = [_window("我们先看这个。好，可以。", [A] * 6 + [B] * 3, start=100.0)]
    _seed(revisions=[("speech_retranscribe", retranscribed), ("ai_polish_after_speech",
                                                               _polished(retranscribed))])

    assert apply_regroup("recording")["status"] == "applied"

    transcript, _source = _load_transcript("recording", regroup_db)
    assert [(s.speaker, s.text) for s in transcript.segments] == [
        (A, "我們先看這一個。"),
        (B, "好的，可以。"),
    ]
    with session_scope() as session:
        raw = session.scalar(select(Transcript))
        latest = session.scalars(
            select(TranscriptRevision).order_by(TranscriptRevision.revision)
        ).all()[-1]
        assert latest.kind == "ai_polish_after_speech"
        assert latest.resolved_profile_snapshot["correction_input"] == {
            "revision": 3,
            "kind": "speech_retranscribe",
            "raw_sha256": raw_transcript_fingerprint(raw),
        }
