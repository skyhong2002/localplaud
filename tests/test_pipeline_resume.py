"""Pipeline resumability: stages are skipped when their artifact exists, and
recomputed with force. Uses fake providers so it's fast and network-free."""

from __future__ import annotations

import pytest


def _reset_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'p.db'}")
    monkeypatch.setenv("LOCALPLAUD_ASR__PROVIDER", "faster-whisper")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONVERT", "false")  # skip ffmpeg
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__POLISH", "false")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)


def _install_fakes(monkeypatch, counters):
    from localplaud.asr.base import Segment, Transcript

    def fake_asr(wav, settings):
        counters["asr"] += 1
        return Transcript(
            segments=[
                Segment(text="hello world", start=0.0, end=1.0, speaker="SPEAKER_00"),
                Segment(text="again", start=1.2, end=2.0, speaker="SPEAKER_00"),
            ],
            language="en",
            provider="fake",
            has_speakers=True,
        )

    def fake_summary(transcript, settings):
        counters["sum"] += 1
        return {
            "title": "T",
            "content_md": "# T\n\nbody",
            "provider": "fake",
            "model": "m",
            "template": settings.pipeline.summary_template,
        }

    def fake_mindmap(transcript, settings, summary_md=None):
        counters["mm"] += 1
        return {
            "template": "mind_map",
            "title": None,
            "content_md": "# T\n- point",
            "provider": "fake",
            "model": "m",
            "detail": {"outline_nodes": 2},
        }

    def fake_embed(chunks, settings):
        counters["emb"] += 1
        return [b"\x00\x00\x80?" for _ in chunks], "fake", 1  # one float32 = 1.0

    monkeypatch.setattr("localplaud.worker.pipeline.transcribe.run_asr", fake_asr)
    monkeypatch.setattr("localplaud.worker.pipeline.summarize.summarize", fake_summary)
    monkeypatch.setattr("localplaud.worker.pipeline.mindmap.generate_mind_map", fake_mindmap)
    monkeypatch.setattr("localplaud.worker.pipeline.index.embed_chunks", fake_embed)


def test_pipeline_resumes_and_forces(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageStatus
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_file

    init_db()
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as s:
        s.add(PlaudFile(id="f1", filename="r", status=FileStatus.downloaded, audio_path=str(audio)))

    counters = {"asr": 0, "sum": 0, "mm": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)

    # First run: every stage executes once.
    process_file("f1")
    assert counters == {"asr": 1, "sum": 1, "mm": 1, "emb": 1}
    with session_scope() as s:
        f = s.get(PlaudFile, "f1")
        assert f.status == FileStatus.done
        assert f.transcript is not None and len(f.summaries) == 2 and len(f.chunks) == 1
        assert [segment["text"] for segment in f.local_transcript.segments] == ["hello world again"]
        stages = {run.stage: run for run in f.stage_runs}
        assert stages[StageName.convert].status == StageStatus.skipped
        assert stages[StageName.transcribe].attempts == 1
        assert stages[StageName.transcribe].status == StageStatus.completed
        assert stages[StageName.transcribe].detail["speaker_grouping"]["output_segments"] == 1
        assert stages[StageName.align].status == StageStatus.degraded
        assert stages[StageName.align].detail["forced_alignment"] is False
        assert "no word timestamps" in stages[StageName.align].error
        assert stages[StageName.diarize].detail["provided_by_asr"] is True
        assert stages[StageName.summarize].attempts == 1
        assert stages[StageName.index].attempts == 1
        assert all(run.resolved_profile_snapshot for run in stages.values())
        assert (
            stages[StageName.transcribe].resolved_profile_snapshot["stages"]["transcribe"][
                "connection"
            ]
            == "asr:faster-whisper"
        )
        assert f.local_transcript.resolved_profile_snapshot
        assert all(summary.resolved_profile_snapshot for summary in f.summaries)
        assert all(chunk.resolved_profile_snapshot for chunk in f.chunks)

    # Second run without force: all stages skipped (artifacts reused).
    process_file("f1")
    assert counters == {"asr": 1, "sum": 1, "mm": 1, "emb": 1}
    with session_scope() as s:
        stages = {run.stage: run for run in s.get(PlaudFile, "f1").stage_runs}
        assert stages[StageName.transcribe].attempts == 1
        assert stages[StageName.align].attempts == 1
        assert stages[StageName.summarize].attempts == 1
        assert stages[StageName.index].attempts == 1

    # With force: everything recomputes.
    process_file("f1", force=True)
    assert counters == {"asr": 2, "sum": 2, "mm": 2, "emb": 2}
    with session_scope() as s:
        stages = {run.stage: run for run in s.get(PlaudFile, "f1").stage_runs}
        assert stages[StageName.transcribe].attempts == 2
        assert stages[StageName.align].attempts == 2
        assert stages[StageName.summarize].attempts == 2
        assert stages[StageName.index].attempts == 2


def test_derived_artifacts_run_without_audio_and_preserve_upstream_stages(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from sqlalchemy import select

    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageAttempt,
        StageName,
        StageRun,
        StageStatus,
    )
    from localplaud.db.models import Transcript as TranscriptRow
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_derived_artifacts

    init_db()
    upstream = {
        StageName.convert: (StageStatus.skipped, 0),
        StageName.transcribe: (StageStatus.completed, 2),
        StageName.align: (StageStatus.degraded, 1),
        StageName.diarize: (StageStatus.completed, 3),
        StageName.correct: (StageStatus.skipped, 0),
    }
    with session_scope() as session:
        recording = PlaudFile(
            id="derived-only",
            filename="derived-only",
            status=FileStatus.done,
            audio_path=None,
            wav_path=None,
        )
        session.add(recording)
        session.add(
            TranscriptRow(
                file_id=recording.id,
                provider="fake-asr",
                model="fake-model",
                source="local",
                language="en",
                text="canonical local transcript",
                segments=[
                    {
                        "text": "canonical local transcript",
                        "start": 0.0,
                        "end": 2.0,
                        "speaker": "SPEAKER_00",
                    }
                ],
                has_speakers=True,
            )
        )
        for stage, (status, attempts) in upstream.items():
            session.add(
                StageRun(
                    file_id=recording.id,
                    stage=stage,
                    status=status,
                    attempts=attempts,
                    provider="upstream-provider",
                    model="upstream-model",
                    artifact_source="local",
                    detail={"sentinel": stage.value},
                    resolved_profile_snapshot={"sentinel": stage.value},
                    error="degraded evidence" if status == StageStatus.degraded else None,
                )
            )

    counters = {"asr": 0, "sum": 0, "mm": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)

    process_derived_artifacts("derived-only")

    assert counters == {"asr": 0, "sum": 1, "mm": 1, "emb": 1}
    with session_scope() as session:
        recording = session.get(PlaudFile, "derived-only")
        assert recording.status == FileStatus.done
        assert recording.audio_path is None and recording.wav_path is None
        assert recording.processing_token is None and recording.processing_lease_until is None
        assert {summary.template for summary in recording.summaries} == {
            "default",
            "mind_map",
        }
        assert recording.chunks
        assert all(summary.input_transcript_source == "local" for summary in recording.summaries)
        assert all(chunk.input_transcript_source == "local" for chunk in recording.chunks)

        stages = {run.stage: run for run in recording.stage_runs}
        for stage, (status, attempts) in upstream.items():
            run = stages[stage]
            assert run.status == status
            assert run.attempts == attempts
            assert run.provider == "upstream-provider"
            assert run.model == "upstream-model"
            assert run.detail == {"sentinel": stage.value}
            assert run.resolved_profile_snapshot == {"sentinel": stage.value}
        assert stages[StageName.summarize].attempts == 1
        assert stages[StageName.mind_map].attempts == 1
        assert stages[StageName.index].attempts == 1

        upstream_attempts = list(
            session.scalars(
                select(StageAttempt).where(
                    StageAttempt.file_id == recording.id,
                    StageAttempt.stage.in_(tuple(upstream)),
                )
            )
        )
        assert upstream_attempts == []


def test_derived_generation_uses_one_shot_profile_in_run_and_attempt_snapshots(
    monkeypatch, tmp_path
):
    _reset_db(monkeypatch, tmp_path)
    from sqlalchemy import select

    from localplaud.db.models import (
        ExecutionProfile,
        FileStatus,
        PlaudFile,
        ProfileStageSelection,
        RecordingProfileOverride,
        StageAttempt,
        StageName,
        StageRun,
        Transcript,
    )
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_derived_artifacts

    init_db()
    with session_scope() as session:
        system = session.scalar(
            select(ExecutionProfile).where(ExecutionProfile.is_system_default.is_(True))
        )
        explicit = ExecutionProfile(
            key="one-shot-notes",
            name="One-shot notes",
            version=1,
        )
        explicit.stage_selections = [
            ProfileStageSelection(
                stage=selection.stage,
                connection_id=selection.connection_id,
                model_id=selection.model_id,
                options=dict(selection.options or {}),
            )
            for selection in system.stage_selections
        ]
        session.add(explicit)
        session.add(
            PlaudFile(
                id="one-shot",
                filename="one-shot",
                status=FileStatus.done,
                transcripts=[
                    Transcript(
                        provider="fake-asr",
                        model="fake-model",
                        source="local",
                        language="en",
                        text="canonical local transcript",
                        segments=[
                            {
                                "text": "canonical local transcript",
                                "start": 0.0,
                                "end": 2.0,
                            }
                        ],
                    )
                ],
            )
        )
        session.flush()
        explicit_id = explicit.id

    counters = {"asr": 0, "sum": 0, "mm": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)
    process_derived_artifacts("one-shot", profile_id=explicit_id)

    with session_scope() as session:
        assert session.get(RecordingProfileOverride, "one-shot") is None
        for stage in (StageName.summarize, StageName.mind_map, StageName.index):
            run = session.scalar(
                select(StageRun).where(
                    StageRun.file_id == "one-shot", StageRun.stage == stage
                )
            )
            attempt = session.scalar(
                select(StageAttempt).where(
                    StageAttempt.file_id == "one-shot", StageAttempt.stage == stage
                )
            )
            for snapshot in (
                run.resolved_profile_snapshot,
                attempt.resolved_profile_snapshot,
            ):
                explicit_layer = snapshot["layer_provenance"][-1]
                assert explicit_layer["kind"] == "explicit_generation"
                assert explicit_layer["profile_id"] == explicit_id


def test_derived_artifacts_require_canonical_local_transcript(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_derived_artifacts

    init_db()
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="derived-missing-transcript",
                filename="derived-missing-transcript",
                status=FileStatus.done,
                audio_path=None,
                wav_path=None,
            )
        )

    with pytest.raises(ValueError, match="canonical local transcript required"):
        process_derived_artifacts("derived-missing-transcript")

    with session_scope() as session:
        recording = session.get(PlaudFile, "derived-missing-transcript")
        assert recording.status == FileStatus.error
        assert recording.processing_token is None and recording.processing_lease_until is None
        assert recording.stage_runs == []


def test_diarization_resume_uses_raw_transcript_not_canonical_revision(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile, TranscriptRevision
    from localplaud.db.models import Transcript as TranscriptRow
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_file

    for stage in ("ALIGN", "SUMMARIZE", "MIND_MAP", "INDEX"):
        monkeypatch.setenv(f"LOCALPLAUD_PIPELINE__{stage}", "false")
    get_settings(reload=True)
    init_db()
    audio = tmp_path / "raw-lane.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as session:
        row = PlaudFile(
            id="raw-lane",
            filename="raw-lane",
            status=FileStatus.downloaded,
            audio_path=str(audio),
        )
        session.add(row)
        raw = TranscriptRow(
            file_id=row.id,
            provider="fake-asr",
            source="local",
            text="raw words",
            segments=[{"text": "raw words", "start": 0.0, "end": 1.0, "speaker": None}],
            has_speakers=False,
        )
        session.add(raw)
        session.flush()
        session.add(
            TranscriptRevision(
                file_id=row.id,
                base_transcript_id=raw.id,
                revision=1,
                source="local",
                text="corrected words",
                segments=[
                    {
                        "text": "corrected words",
                        "start": 0.0,
                        "end": 1.0,
                        "speaker": None,
                    }
                ],
                kind="user_edit",
                has_speakers=False,
            )
        )

    seen = []

    def fake_diarize(_wav, transcript, _cfg):
        seen.append(transcript.text)
        for segment in transcript.segments:
            segment.speaker = "SPEAKER_00"
        transcript.has_speakers = True
        return transcript

    monkeypatch.setattr("localplaud.worker.pipeline.diarize", fake_diarize)

    process_file("raw-lane")

    assert seen == ["raw words"]
    with session_scope() as session:
        row = session.get(PlaudFile, "raw-lane")
        assert row.local_transcript.text == "raw words"
        assert row.corrected_transcript.text == "corrected words"


def test_index_failure_keeps_transcript_and_summary(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageStatus
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_file

    init_db()
    audio = tmp_path / "index-failure.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as s:
        s.add(PlaudFile(id="index-failure", status=FileStatus.downloaded, audio_path=str(audio)))

    counters = {"asr": 0, "sum": 0, "mm": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)

    def fail_embed(chunks, settings):
        counters["emb"] += 1
        raise RuntimeError("embedding model unavailable")

    monkeypatch.setattr("localplaud.worker.pipeline.index.embed_chunks", fail_embed)
    process_file("index-failure")

    with session_scope() as s:
        f = s.get(PlaudFile, "index-failure")
        assert f.status == FileStatus.partial
        assert f.local_transcript is not None
        assert {s.template for s in f.summaries} == {"default", "mind_map"}
        assert len(f.chunks) == 0
        index_run = next(x for x in f.stage_runs if x.stage == StageName.index)
        assert index_run.status == StageStatus.failed
        assert index_run.attempts == 1
        assert "embedding model unavailable" in index_run.error
        assert f.pipeline_retry_count == 1
        assert f.pipeline_next_retry_at is not None


def test_asr_failure_marks_core_pipeline_error(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageStatus
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_file

    init_db()
    audio = tmp_path / "asr-failure.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as s:
        s.add(PlaudFile(id="asr-failure", status=FileStatus.downloaded, audio_path=str(audio)))

    def fail_asr(wav, settings):
        raise RuntimeError("ASR model unavailable")

    monkeypatch.setattr("localplaud.worker.pipeline.transcribe.run_asr", fail_asr)
    with pytest.raises(RuntimeError, match="ASR model unavailable"):
        process_file("asr-failure")

    with session_scope() as s:
        f = s.get(PlaudFile, "asr-failure")
        assert f.status == FileStatus.error
        assert f.local_transcript is None
        transcribe_run = next(x for x in f.stage_runs if x.stage == StageName.transcribe)
        assert transcribe_run.status == StageStatus.failed
        assert transcribe_run.attempts == 1
        assert f.pipeline_retry_count == 1
        assert f.pipeline_next_retry_at is not None


def test_pending_batch_prioritizes_newest_recordings(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_pending

    init_db()
    with session_scope() as s:
        for file_id, started in (("old", 100), ("new", 300), ("middle", 200)):
            s.add(
                PlaudFile(
                    id=file_id,
                    status=FileStatus.downloaded,
                    audio_path=f"/{file_id}.wav",
                    start_time_ms=started,
                )
            )

    processed = []
    monkeypatch.setattr(
        "localplaud.worker.pipeline.process_file",
        lambda file_id, settings, force=False: processed.append(file_id),
    )
    assert process_pending(limit=2) == 2
    assert processed == ["new", "middle"]


def test_pending_batch_propagates_daemon_owner_to_worker_threads(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.claims import current_processing_owner, processing_owner
    from localplaud.worker.pipeline import process_pending

    settings = get_settings()
    settings.pipeline.concurrency = 2
    init_db()
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id=f"owned-{index}",
                    status=FileStatus.downloaded,
                    audio_path=f"/owned-{index}.wav",
                    start_time_ms=index,
                )
                for index in range(2)
            ]
        )

    observed: list[str | None] = []
    monkeypatch.setattr(
        "localplaud.worker.pipeline.process_file",
        lambda _file_id, _settings, force=False: observed.append(current_processing_owner()),
    )
    with processing_owner("daemon-owner"):
        assert process_pending(settings) == 2
    assert observed == ["daemon-owner", "daemon-owner"]


def test_processing_claim_token_carries_daemon_owner_within_column_limit(
    monkeypatch, tmp_path
):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.claims import processing_owner
    from localplaud.worker.pipeline import _claim_processing, _release_processing

    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="owned-claim", status=FileStatus.done))
    with processing_owner("0123456789abcdef"):
        token = _claim_processing("owned-claim", require_audio=False, mark_processing=False)
    assert token.startswith("daemon:0123456789abcdef:")
    assert len(token) <= 64
    _release_processing("owned-claim", token)


def test_displaced_pipeline_cannot_publish_or_mark_failure(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime, timedelta
    from threading import Event

    _reset_db(monkeypatch, tmp_path)
    from localplaud.asr.base import Segment, Transcript
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageStatus
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import PipelineAlreadyRunning, process_file

    init_db()
    audio = tmp_path / "takeover.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="takeover",
                status=FileStatus.downloaded,
                audio_path=str(audio),
            )
        )

    provider_started = Event()
    provider_return = Event()

    def delayed_asr(_wav, _settings):
        provider_started.set()
        assert provider_return.wait(5)
        return Transcript(
            segments=[Segment(text="stale result", start=0.0, end=1.0)],
            provider="old-worker",
            model="old-model",
        )

    monkeypatch.setattr("localplaud.worker.pipeline.transcribe.run_asr", delayed_asr)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_worker = pool.submit(process_file, "takeover")
        assert provider_started.wait(5)
        with session_scope() as session:
            recording = session.get(PlaudFile, "takeover")
            recording.processing_token = "new-owner-token"
            recording.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)
            recording.status = FileStatus.processing
            recording.error = None
        provider_return.set()
        with pytest.raises(PipelineAlreadyRunning, match="no longer active"):
            old_worker.result(timeout=5)

    with session_scope() as session:
        recording = session.get(PlaudFile, "takeover")
        assert recording.processing_token == "new-owner-token"
        assert recording.status == FileStatus.processing
        assert recording.error is None
        assert recording.local_transcript is None
        transcribe_run = next(
            run for run in recording.stage_runs if run.stage == StageName.transcribe
        )
        assert transcribe_run.status == StageStatus.running


def test_displaced_conversion_never_publishes_partial_wav(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime, timedelta
    from threading import Event

    _reset_db(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONVERT", "true")
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.store.files import wav_path
    from localplaud.worker.pipeline import PipelineAlreadyRunning, process_file

    get_settings(reload=True)
    init_db()
    audio = tmp_path / "source.opus"
    audio.write_bytes(b"raw")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="wav-takeover",
                status=FileStatus.downloaded,
                audio_path=str(audio),
            )
        )

    staged = Event()
    release = Event()

    def partial_convert(_source, destination):
        destination.write_bytes(b"partial-old-owner")
        staged.set()
        assert release.wait(5)

    monkeypatch.setattr("localplaud.worker.pipeline.convert.to_wav", partial_convert)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_worker = pool.submit(process_file, "wav-takeover")
        assert staged.wait(5)
        with session_scope() as session:
            row = session.get(PlaudFile, "wav-takeover")
            row.processing_token = "replacement"
            row.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)
        release.set()
        with pytest.raises(PipelineAlreadyRunning, match="no longer active"):
            old_worker.result(timeout=5)

    final_wav = wav_path("wav-takeover")
    assert not final_wav.exists()
    assert list(final_wav.parent.glob(".*.tmp.wav")) == []
    with session_scope() as session:
        row = session.get(PlaudFile, "wav-takeover")
        assert row.processing_token == "replacement"
        assert row.wav_path is None


def test_vocabulary_detail_write_is_fenced_after_takeover(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, StageName
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import PipelineAlreadyRunning, process_file

    init_db()
    audio = tmp_path / "vocabulary.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="vocabulary-takeover",
                status=FileStatus.downloaded,
                audio_path=str(audio),
            )
        )
    counters = {"asr": 0, "sum": 0, "mm": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)

    def displace_then_report(file_id, **_kwargs):
        with session_scope() as session:
            row = session.get(PlaudFile, file_id)
            row.processing_token = "replacement"
            row.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)
        return {"replacements": 1, "revision": 1}

    monkeypatch.setattr("localplaud.vocabulary.apply_vocabulary", displace_then_report)
    with pytest.raises(PipelineAlreadyRunning, match="no longer active"):
        process_file("vocabulary-takeover")
    with session_scope() as session:
        row = session.get(PlaudFile, "vocabulary-takeover")
        run = next(item for item in row.stage_runs if item.stage == StageName.transcribe)
        assert "vocabulary" not in (run.detail or {})
        assert row.processing_token == "replacement"


def test_note_index_handoff_asserts_claim_before_independent_work(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import PipelineAlreadyRunning, process_file

    init_db()
    audio = tmp_path / "handoff.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as session:
        session.add(
            PlaudFile(id="handoff", status=FileStatus.downloaded, audio_path=str(audio))
        )
    counters = {"asr": 0, "sum": 0, "mm": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)
    provider_calls = 0

    def stale_claim(*_args, **_kwargs):
        raise PipelineAlreadyRunning("processing claim for handoff is no longer active")

    def note_provider(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return 0

    monkeypatch.setattr("localplaud.worker.pipeline._assert_processing_claim", stale_claim)
    monkeypatch.setattr(
        "localplaud.worker.knowledge_index.process_file_documents", note_provider
    )
    with pytest.raises(PipelineAlreadyRunning, match="no longer active"):
        process_file("handoff")
    assert provider_calls == 0


def test_pending_batch_resumes_audio_less_derived_retry(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
    )
    from localplaud.db.models import Transcript as TranscriptRow
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_pending

    init_db()
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id="derived-retry",
                    status=FileStatus.partial,
                    transcripts=[
                        TranscriptRow(
                            source="local",
                            provider="test",
                            text="canonical transcript",
                            segments=[
                                {"text": "canonical transcript", "start": 0.0, "end": 1.0}
                            ],
                        )
                    ],
                    stage_runs=[
                        StageRun(
                            stage=StageName.summarize,
                            status=StageStatus.failed,
                            detail={"stale": True, "derived_only": True},
                        )
                    ],
                ),
                PlaudFile(id="metadata-only-error", status=FileStatus.error),
            ]
        )

    derived = []
    full = []
    monkeypatch.setattr(
        "localplaud.worker.pipeline.process_derived_artifacts",
        lambda file_id, settings: derived.append(file_id),
    )
    monkeypatch.setattr(
        "localplaud.worker.pipeline.process_file",
        lambda file_id, settings, force=False: full.append(file_id),
    )
    assert process_pending() == 1
    assert derived == ["derived-retry"]
    assert full == []


def test_independent_mode_ignores_but_preserves_cloud_transcript(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.models import Transcript as TranscriptRow
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_file

    init_db()
    audio = tmp_path / "cloud.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as s:
        s.add(
            PlaudFile(
                id="cloud-only",
                filename="r",
                status=FileStatus.downloaded,
                audio_path=str(audio),
                transcripts=[
                    TranscriptRow(
                        provider="plaud",
                        source="cloud",
                        text="paid cloud text",
                        segments=[{"text": "paid cloud text", "start": 0.0, "end": 1.0}],
                    )
                ],
            )
        )

    counters = {"asr": 0, "sum": 0, "mm": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)
    process_file("cloud-only")
    assert counters == {"asr": 1, "sum": 1, "mm": 1, "emb": 1}

    with session_scope() as s:
        f = s.get(PlaudFile, "cloud-only")
        assert {t.source for t in f.transcripts} == {"cloud", "local"}
        assert f.transcript is not None
        assert f.transcript.source == "local"
        assert f.transcript.text == "hello world again"

    # Resume uses the local transcript and never falls back to the Plaud copy.
    process_file("cloud-only")
    assert counters == {"asr": 1, "sum": 1, "mm": 1, "emb": 1}


def test_migration_mode_can_explicitly_reuse_cloud_transcript(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__ARTIFACT_MODE", "migration")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__PREFER_CLOUD_ARTIFACTS", "true")
    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.models import Transcript as TranscriptRow
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_file

    settings = get_settings(reload=True)
    init_db()
    audio = tmp_path / "migration.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as s:
        s.add(
            PlaudFile(
                id="migration",
                status=FileStatus.downloaded,
                audio_path=str(audio),
                transcripts=[
                    TranscriptRow(
                        provider="plaud",
                        source="cloud",
                        text="cloud text",
                        segments=[{"text": "cloud text", "start": 0.0, "end": 1.0}],
                    )
                ],
            )
        )

    counters = {"asr": 0, "sum": 0, "mm": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)
    process_file("migration", settings=settings)
    assert counters == {"asr": 0, "sum": 1, "mm": 1, "emb": 1}
    with session_scope() as s:
        assert [t.source for t in s.get(PlaudFile, "migration").transcripts] == ["cloud"]
