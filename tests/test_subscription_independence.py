"""Product-gate acceptance for a clean raw-audio recording."""

from __future__ import annotations

import json

from typer.testing import CliRunner


def _setup(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'gate.db'}")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONVERT", "false")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    return get_settings(reload=True)


def _providers(monkeypatch):
    from localplaud.asr.base import Segment, Transcript, Word

    monkeypatch.setattr(
        "localplaud.worker.pipeline.transcribe.run_asr",
        lambda *_args: Transcript(
            segments=[
                Segment(
                    text="今天確認本機處理流程。",
                    start=0.0,
                    end=2.0,
                    speaker="SPEAKER_00",
                    words=[
                        Word(
                            text="今天確認本機處理流程。",
                            start=0.0,
                            end=2.0,
                            speaker="SPEAKER_00",
                        )
                    ],
                )
            ],
            language="zh",
            provider="acceptance-fake",
            model="large-v3-turbo",
            has_speakers=True,
        ),
    )
    monkeypatch.setattr(
        "localplaud.worker.pipeline.summarize.summarize",
        lambda transcript, settings: {
            "title": "本機筆記",
            "content_md": "# 本機筆記\n\n- 完整涵蓋",
            "provider": "acceptance-fake",
            "model": "local-llm",
            "template": settings.pipeline.summary_template,
        },
    )
    monkeypatch.setattr(
        "localplaud.worker.pipeline.mindmap.generate_mind_map",
        lambda *_args, **_kwargs: {
            "template": "mind_map",
            "title": None,
            "content_md": "# 心智圖\n- 本機流程",
            "provider": "acceptance-fake",
            "model": "local-llm",
            "detail": {"outline_nodes": 2},
        },
    )
    monkeypatch.setattr(
        "localplaud.worker.pipeline.index.embed_chunks",
        lambda chunks, settings: ([b"\x00\x00\x80?" for _ in chunks], "local-test", 1),
    )
    monkeypatch.setattr(
        "localplaud.worker.pipeline.polish.polish_transcript",
        lambda transcript, _settings: {
            "transcript": transcript,
            "provider": "opencode-go",
            "model": "qwen3.7-plus",
            "prompt_version": "transcript-polish/v1",
            "detail": {
                "chunks": 1,
                "segments": len(transcript.segments),
                "input_chars": len(transcript.text),
                "output_chars": len(transcript.text),
            },
        },
    )


def test_clean_raw_audio_passes_subscription_independence_gate(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from localplaud.acceptance import subscription_independence_report
    from localplaud.cli import app
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageStatus,
    )
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_file

    init_db()
    audio = tmp_path / "clean.wav"
    audio.write_bytes(b"RIFF-local-user-owned-audio")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="clean",
                filename="Clean raw recording",
                status=FileStatus.downloaded,
                audio_path=str(audio),
            )
        )
    _providers(monkeypatch)
    process_file("clean")
    with session_scope() as session:
        row = session.get(PlaudFile, "clean")
        polished = row.corrected_transcript
        assert polished.kind == "ai_polish"
        assert polished.provider == "opencode-go"
        assert polished.model == "qwen3.7-plus"
        assert polished.prompt_version == "transcript-polish/v1"
        assert polished.resolved_profile_snapshot["stages"]["correct"]["connection"].endswith(
            "opencode-go"
        )
        assert all(summary.input_transcript_revision == 1 for summary in row.summaries)
        alignment = next(stage for stage in row.stage_runs if stage.stage == StageName.align)
        assert alignment.status == StageStatus.completed
        assert alignment.detail["strategy"] == "provider-word-timestamps"
        assert alignment.detail["forced_alignment"] is False
        correct = next(stage for stage in row.stage_runs if stage.stage.value == "correct")
        assert correct.status.value == "completed"
        assert correct.provider == "opencode-go"

    report = subscription_independence_report("clean")
    assert report["schema"] == "localplaud-subscription-independence/v1"
    assert report["passed"] is True
    assert {item["name"] for item in report["checks"]} == {
        "raw_audio_local",
        "local_transcript",
        "transcript_polish",
        "timestamped_segments",
        "word_alignment",
        "speaker_assignment",
        "speaker_diarization",
        "local_notes",
        "local_mind_map",
        "ask_index",
        "durable_stages",
        "required_exports",
    }
    assert all(item["passed"] for item in report["checks"])

    # Exercise the user-facing grounded Ask path from the locally indexed recording.
    class LocalAskModel:
        def complete(self, *_args, **_kwargs):
            return "本機流程已確認。"

    monkeypatch.setattr(
        "localplaud.worker.qa.retrieve",
        lambda *_args, **_kwargs: [
            {
                "file_id": "clean",
                "filename": "Clean raw recording",
                "start": 0.0,
                "end": 2.0,
                "speaker": "SPEAKER_00",
                "text": "今天確認本機處理流程。",
                "score": 1.0,
            }
        ],
    )
    monkeypatch.setattr("localplaud.worker.qa.build_llm", lambda *_args: LocalAskModel())
    from localplaud.ask_threads import ask_in_thread

    thread = ask_in_thread("確認了什麼？", file_id="clean")
    answer = thread["messages"][-1]
    assert answer["content"] == "本機流程已確認。"
    assert answer["sources"][0]["file_id"] == "clean"
    assert answer["sources"][0]["start"] == 0.0

    result = CliRunner().invoke(app, ["acceptance-check", "clean", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["passed"] is True

    # Local provenance alone is not enough: a stale note from raw revision 0
    # must not satisfy a gate whose canonical input is AI-polished revision 1.
    with session_scope() as session:
        row = session.get(PlaudFile, "clean")
        note = next(item for item in row.summaries if item.template != "mind_map")
        note.input_transcript_revision = 0
    stale_report = subscription_independence_report("clean")
    stale_checks = {item["name"]: item["passed"] for item in stale_report["checks"]}
    assert stale_checks["local_notes"] is False
    assert stale_checks["local_mind_map"] is True
    assert stale_checks["ask_index"] is True


def test_cloud_only_recording_fails_gate_with_actionable_checks(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    from localplaud.acceptance import subscription_independence_report
    from localplaud.db.models import FileStatus, PlaudFile, Transcript
    from localplaud.db.session import init_db, session_scope

    init_db()
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="cloud-only",
                status=FileStatus.metadata_only,
                transcripts=[
                    Transcript(
                        provider="plaud",
                        source="cloud",
                        text="paid artifact",
                        segments=[{"text": "paid artifact", "start": 0.0, "end": 1.0}],
                    )
                ],
            )
        )
    report = subscription_independence_report("cloud-only")
    failed = {item["name"] for item in report["checks"] if not item["passed"]}
    assert report["passed"] is False
    assert {"raw_audio_local", "local_transcript", "required_exports"} <= failed


def test_polish_failure_blocks_notes_and_index_but_keeps_raw_transcript(
    monkeypatch, tmp_path
):
    _setup(monkeypatch, tmp_path)
    from localplaud.acceptance import subscription_independence_report
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageStatus
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_file

    init_db()
    audio = tmp_path / "polish-failure.wav"
    audio.write_bytes(b"RIFF-local-user-owned-audio")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="polish-failure",
                status=FileStatus.downloaded,
                audio_path=str(audio),
            )
        )
    _providers(monkeypatch)
    monkeypatch.setattr(
        "localplaud.worker.pipeline.polish.polish_transcript",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider down")),
    )

    process_file("polish-failure")
    with session_scope() as session:
        row = session.get(PlaudFile, "polish-failure")
        assert row.status == FileStatus.partial
        assert row.local_transcript is not None
        assert row.corrected_transcript is None
        assert row.summaries == [] and row.chunks == []
        correct = next(stage for stage in row.stage_runs if stage.stage == StageName.correct)
        assert correct.status == StageStatus.failed
        assert "provider down" in correct.error

    report = subscription_independence_report("polish-failure")
    polish_check = next(
        item for item in report["checks"] if item["name"] == "transcript_polish"
    )
    assert polish_check == {
        "name": "transcript_polish",
        "passed": False,
        "detail": "no local AI-polished transcript revision",
    }
