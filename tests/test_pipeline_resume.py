"""Pipeline resumability: stages are skipped when their artifact exists, and
recomputed with force. Uses fake providers so it's fast and network-free."""

from __future__ import annotations

import pytest


def _reset_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'p.db'}")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONVERT", "false")  # skip ffmpeg
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)


def _install_fakes(monkeypatch, counters):
    from localplaud.asr.base import Segment, Transcript, Word

    def fake_asr(wav, settings):
        counters["asr"] += 1
        return Transcript(
            segments=[Segment(text="hello world", start=0.0, end=1.0,
                              speaker="SPEAKER_00",
                              words=[Word(text="hello", start=0.0, end=0.5,
                                          speaker="SPEAKER_00")])],
            language="en", provider="fake", has_speakers=True,
        )

    def fake_summary(transcript, settings):
        counters["sum"] += 1
        return {"title": "T", "content_md": "# T\n\nbody", "provider": "fake",
                "model": "m", "template": settings.pipeline.summary_template}

    def fake_mindmap(transcript, settings, summary_md=None):
        counters["mm"] += 1
        return {"template": "mind_map", "title": None, "content_md": "# T\n- point",
                "provider": "fake", "model": "m", "detail": {"outline_nodes": 2}}

    def fake_embed(chunks, settings):
        counters["emb"] += 1
        return [b"\x00\x00\x80?" for _ in chunks], "fake", 1  # one float32 = 1.0

    monkeypatch.setattr("localplaud.worker.pipeline.transcribe.run_asr", fake_asr)
    monkeypatch.setattr("localplaud.worker.pipeline.summarize.summarize", fake_summary)
    monkeypatch.setattr("localplaud.worker.pipeline.mindmap.generate_mind_map", fake_mindmap)
    monkeypatch.setattr("localplaud.worker.pipeline.index.embed_chunks", fake_embed)


def test_pipeline_resumes_and_forces(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, StageAttempt, StageName, StageStatus
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
        assert [row.artifact_kind for row in f.transcripts] == ["raw_asr", "aligned"]
        assert f.raw_asr_transcript is not None
        assert f.local_transcript.artifact_kind == "aligned"
        stages = {run.stage: run for run in f.stage_runs}
        assert stages[StageName.convert].status == StageStatus.skipped
        assert stages[StageName.transcribe].attempts == 1
        assert stages[StageName.transcribe].status == StageStatus.completed
        from localplaud.config import get_settings

        assert (
            stages[StageName.transcribe].profile_snapshot["route"]["provider"]
            == get_settings().asr.provider
        )
        # The system boundary permits explicitly selected cloud/worker starters;
        # the active default route itself remains the configured local provider.
        assert stages[StageName.transcribe].profile_snapshot["policy"]["no_egress"] is False
        assert stages[StageName.transcribe].profile_snapshot["route"]["egress"] == "local_only"
        assert stages[StageName.diarize].detail["provided_by_asr"] is True
        assert stages[StageName.summarize].attempts == 1
        assert stages[StageName.index].attempts == 1
        history = list(s.query(StageAttempt).filter_by(file_id="f1",
                                                       stage=StageName.transcribe))
        assert len(history) == 1
        assert history[0].profile_snapshot == stages[StageName.transcribe].profile_snapshot

    # Second run without force: all stages skipped (artifacts reused).
    process_file("f1")
    assert counters == {"asr": 1, "sum": 1, "mm": 1, "emb": 1}
    with session_scope() as s:
        stages = {run.stage: run for run in s.get(PlaudFile, "f1").stage_runs}
        assert stages[StageName.transcribe].attempts == 1
        assert stages[StageName.summarize].attempts == 1
        assert stages[StageName.index].attempts == 1

    # With force: everything recomputes.
    process_file("f1", force=True)
    assert counters == {"asr": 2, "sum": 2, "mm": 2, "emb": 2}
    with session_scope() as s:
        stages = {run.stage: run for run in s.get(PlaudFile, "f1").stage_runs}
        assert stages[StageName.transcribe].attempts == 2
        assert stages[StageName.summarize].attempts == 2
        assert stages[StageName.index].attempts == 2
        history = list(s.query(StageAttempt).filter_by(file_id="f1",
                                                       stage=StageName.transcribe).order_by(
                                                           StageAttempt.attempt))
        assert [item.attempt for item in history] == [1, 2]
        assert all(item.status == StageStatus.completed for item in history)


def test_processing_claim_is_atomic(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import ProcessingAlreadyRunning, _claim_processing

    init_db()
    audio = tmp_path / "claim.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as session:
        session.add(PlaudFile(id="claim", status=FileStatus.downloaded,
                              audio_path=str(audio)))
    token = _claim_processing("claim", get_settings())
    assert token
    with pytest.raises(ProcessingAlreadyRunning):
        _claim_processing("claim", get_settings())


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
        assert f.retry_count == 1
        assert f.retry_after is not None


def test_due_partial_retry_resumes_only_failed_downstream_stage(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from datetime import UTC, datetime, timedelta

    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageStatus
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_file, process_pending

    init_db()
    audio = tmp_path / "partial-retry.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as session:
        session.add(PlaudFile(id="partial-retry", status=FileStatus.downloaded,
                              audio_path=str(audio)))
    counters = {"asr": 0, "sum": 0, "mm": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)
    original_embed = __import__("localplaud.worker.pipeline", fromlist=["index"]).index.embed_chunks
    monkeypatch.setattr(
        "localplaud.worker.pipeline.index.embed_chunks",
        lambda *args: (_ for _ in ()).throw(RuntimeError("temporary index failure")),
    )
    process_file("partial-retry")
    assert counters == {"asr": 1, "sum": 1, "mm": 1, "emb": 0}
    with session_scope() as session:
        row = session.get(PlaudFile, "partial-retry")
        row.retry_after = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr("localplaud.worker.pipeline.index.embed_chunks", original_embed)
    assert process_pending() == 1
    assert counters == {"asr": 1, "sum": 1, "mm": 1, "emb": 1}
    with session_scope() as session:
        row = session.get(PlaudFile, "partial-retry")
        assert row.status == FileStatus.done
        assert row.retry_count == 0 and row.retry_after is None
        assert len(row.summaries) == 2 and len(row.chunks) == 1
        runs = {run.stage: run for run in row.stage_runs}
        assert runs[StageName.transcribe].attempts == 1
        assert runs[StageName.index].attempts == 2
        assert runs[StageName.index].status == StageStatus.completed


def test_partial_pending_requires_due_failed_stage_and_stops_when_exhausted(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from datetime import UTC, datetime, timedelta

    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_pending

    init_db()
    now = datetime.now(UTC)
    with session_scope() as session:
        for file_id, retry_count, retry_after, stage_status in (
            ("due", 1, now - timedelta(seconds=1), StageStatus.failed),
            ("not-due", 1, now + timedelta(hours=1), StageStatus.failed),
            ("no-failure", 1, now - timedelta(seconds=1), StageStatus.completed),
            ("degraded", 1, now - timedelta(seconds=1), StageStatus.degraded),
            ("exhausted", 3, None, StageStatus.failed),
            ("exhausted-due", 3, now - timedelta(seconds=1), StageStatus.failed),
        ):
            session.add(PlaudFile(id=file_id, status=FileStatus.partial,
                                  audio_path=f"/{file_id}.wav", retry_count=retry_count,
                                  retry_after=retry_after))
            session.add(StageRun(file_id=file_id, stage=StageName.index,
                                 status=stage_status))
    processed = []
    monkeypatch.setattr("localplaud.worker.pipeline.process_file",
                        lambda file_id, settings, force=False: processed.append(file_id))
    assert process_pending() == 1
    assert processed == ["due"]


def test_terminal_stage_attempt_is_not_rewritten_by_duplicate_finish(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, StageAttempt, StageName, StageStatus
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import _begin_stage, _finish_stage

    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="immutable-attempt", status=FileStatus.downloaded))
    _begin_stage("immutable-attempt", StageName.summarize)
    _finish_stage("immutable-attempt", StageName.summarize, provider="first", detail={"one": 1})
    with session_scope() as session:
        original = session.query(StageAttempt).filter_by(file_id="immutable-attempt").one()
        completed_at = original.completed_at
    _finish_stage("immutable-attempt", StageName.summarize, provider="aggregate",
                  detail={"duplicate": True})
    with session_scope() as session:
        attempt = session.query(StageAttempt).filter_by(file_id="immutable-attempt").one()
        run = session.get(PlaudFile, "immutable-attempt").stage_runs[0]
        assert attempt.status == StageStatus.completed
        assert attempt.provider == "first" and attempt.detail == {"one": 1}
        assert attempt.completed_at == completed_at
        assert run.status == StageStatus.completed
        assert run.provider == "aggregate" and run.detail == {"duplicate": True}


def test_new_attempt_does_not_inherit_prior_usage_cost_or_provider(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, StageAttempt, StageName, StageStatus
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import _begin_stage, _fail_stage, _finish_stage

    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="fresh-attempt", status=FileStatus.downloaded))
    _begin_stage("fresh-attempt", StageName.summarize)
    _finish_stage(
        "fresh-attempt", StageName.summarize, provider="first", model="old-model",
        usage={"tokens": 99}, actual_cost=3.5,
    )
    _begin_stage("fresh-attempt", StageName.summarize)
    _fail_stage("fresh-attempt", StageName.summarize, RuntimeError("failed before usage"))
    with session_scope() as session:
        attempts = session.query(StageAttempt).filter_by(
            file_id="fresh-attempt"
        ).order_by(StageAttempt.attempt).all()
        run = session.get(PlaudFile, "fresh-attempt").stage_runs[0]
        assert len(attempts) == 2
        assert attempts[0].provider == "first" and attempts[0].usage == {"tokens": 99}
        assert attempts[0].actual_cost == 3.5
        assert attempts[1].status == StageStatus.failed
        assert attempts[1].provider != "first" and attempts[1].model != "old-model"
        assert attempts[1].usage == {} and attempts[1].actual_cost is None
        assert run.provider is None and run.usage == {} and run.actual_cost is None


def test_pipeline_error_schedules_bounded_exponential_retry(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__RETRY_BASE_SECONDS", "10")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__RETRY_MAX_ATTEMPTS", "2")
    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_file

    settings = get_settings(reload=True)
    init_db()
    audio = tmp_path / "retry.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as session:
        session.add(PlaudFile(id="retry", status=FileStatus.downloaded, audio_path=str(audio)))
    monkeypatch.setattr("localplaud.worker.pipeline.transcribe.run_asr",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("temporary")))
    with pytest.raises(RuntimeError, match="temporary"):
        process_file("retry", settings)
    with session_scope() as session:
        row = session.get(PlaudFile, "retry")
        assert row.status == FileStatus.error
        assert row.retry_count == 1
        assert row.retry_after is not None
    with pytest.raises(RuntimeError, match="temporary"):
        process_file("retry", settings)
    with session_scope() as session:
        row = session.get(PlaudFile, "retry")
        assert row.retry_count == 2
        assert row.retry_after is None


def test_profile_change_only_resolves_on_a_real_new_attempt(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import ExecutionProfile, FileStatus, PlaudFile, StageName
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import _begin_stage, _finish_stage

    init_db()
    with session_scope() as session:
        file = PlaudFile(id="profile-change", status=FileStatus.downloaded)
        session.add(file)
    _begin_stage("profile-change", StageName.transcribe)
    _finish_stage("profile-change", StageName.transcribe, provider="faster-whisper")
    with session_scope() as session:
        file = session.get(PlaudFile, "profile-change")
        run = file.stage_runs[0]
        old_snapshot = run.profile_snapshot
        override = session.query(ExecutionProfile).filter_by(name="apple-local").one()
        file.execution_profile_id = override.id
        run.detail = {"profile_change_pending": True}
    # A reuse completion does not rewrite who selected/produced the artifact.
    _finish_stage("profile-change", StageName.transcribe, provider="faster-whisper",
                  detail={"reused": True, "profile_change_pending": True})
    with session_scope() as session:
        assert session.get(PlaudFile, "profile-change").stage_runs[0].profile_snapshot == old_snapshot
    _begin_stage("profile-change", StageName.transcribe)
    with session_scope() as session:
        run = session.get(PlaudFile, "profile-change").stage_runs[0]
        assert run.profile_snapshot["route"]["provider"] == "mlx-whisper"


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
        assert f.transcript.text == "hello world"

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
