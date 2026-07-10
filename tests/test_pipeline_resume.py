"""Pipeline resumability: stages are skipped when their artifact exists, and
recomputed with force. Uses fake providers so it's fast and network-free."""

from __future__ import annotations


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
    from localplaud.db.models import FileStatus, PlaudFile
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

    # Second run without force: all stages skipped (artifacts reused).
    process_file("f1")
    assert counters == {"asr": 1, "sum": 1, "emb": 1}

    # With force: everything recomputes.
    process_file("f1", force=True)
    assert counters == {"asr": 2, "sum": 2, "emb": 2}
