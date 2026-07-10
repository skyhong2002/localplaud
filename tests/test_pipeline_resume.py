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
    from localplaud.asr.base import Segment, Transcript

    def fake_asr(wav, settings):
        counters["asr"] += 1
        return Transcript(
            segments=[Segment(text="hello world", start=0.0, end=1.0, speaker="SPEAKER_00")],
            language="en", provider="fake", has_speakers=True,
        )

    def fake_summary(transcript, settings):
        counters["sum"] += 1
        return {"title": "T", "content_md": "# T\n\nbody", "provider": "fake",
                "model": "m", "template": settings.pipeline.summary_template}

    def fake_embed(chunks, settings):
        counters["emb"] += 1
        return [b"\x00\x00\x80?" for _ in chunks], "fake", 1  # one float32 = 1.0

    monkeypatch.setattr("localplaud.worker.pipeline.transcribe.run_asr", fake_asr)
    monkeypatch.setattr("localplaud.worker.pipeline.summarize.summarize", fake_summary)
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

    counters = {"asr": 0, "sum": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)

    # First run: every stage executes once.
    process_file("f1")
    assert counters == {"asr": 1, "sum": 1, "emb": 1}
    with session_scope() as s:
        f = s.get(PlaudFile, "f1")
        assert f.status == FileStatus.done
        assert f.transcript is not None and len(f.summaries) == 1 and len(f.chunks) == 1
        stages = {run.stage: run for run in f.stage_runs}
        assert stages[StageName.convert].status == StageStatus.skipped
        assert stages[StageName.transcribe].attempts == 1
        assert stages[StageName.transcribe].status == StageStatus.completed
        assert stages[StageName.diarize].detail["provided_by_asr"] is True
        assert stages[StageName.summarize].attempts == 1
        assert stages[StageName.index].attempts == 1

    # Second run without force: all stages skipped (artifacts reused).
    process_file("f1")
    assert counters == {"asr": 1, "sum": 1, "emb": 1}
    with session_scope() as s:
        stages = {run.stage: run for run in s.get(PlaudFile, "f1").stage_runs}
        assert stages[StageName.transcribe].attempts == 1
        assert stages[StageName.summarize].attempts == 1
        assert stages[StageName.index].attempts == 1

    # With force: everything recomputes.
    process_file("f1", force=True)
    assert counters == {"asr": 2, "sum": 2, "emb": 2}
    with session_scope() as s:
        stages = {run.stage: run for run in s.get(PlaudFile, "f1").stage_runs}
        assert stages[StageName.transcribe].attempts == 2
        assert stages[StageName.summarize].attempts == 2
        assert stages[StageName.index].attempts == 2


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

    counters = {"asr": 0, "sum": 0, "emb": 0}
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
        assert len(f.summaries) == 1
        assert len(f.chunks) == 0
        index_run = next(x for x in f.stage_runs if x.stage == StageName.index)
        assert index_run.status == StageStatus.failed
        assert index_run.attempts == 1
        assert "embedding model unavailable" in index_run.error


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

    counters = {"asr": 0, "sum": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)
    process_file("cloud-only")
    assert counters == {"asr": 1, "sum": 1, "emb": 1}

    with session_scope() as s:
        f = s.get(PlaudFile, "cloud-only")
        assert {t.source for t in f.transcripts} == {"cloud", "local"}
        assert f.transcript is not None
        assert f.transcript.source == "local"
        assert f.transcript.text == "hello world"

    # Resume uses the local transcript and never falls back to the Plaud copy.
    process_file("cloud-only")
    assert counters == {"asr": 1, "sum": 1, "emb": 1}


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

    counters = {"asr": 0, "sum": 0, "emb": 0}
    _install_fakes(monkeypatch, counters)
    process_file("migration", settings=settings)
    assert counters == {"asr": 0, "sum": 1, "emb": 1}
    with session_scope() as s:
        assert [t.source for t in s.get(PlaudFile, "migration").transcripts] == ["cloud"]
