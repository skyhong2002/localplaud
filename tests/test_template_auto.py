"""Explainable local Auto note-template selection and pipeline resolution."""

from __future__ import annotations


def _reset(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'auto.db'}")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONVERT", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__TRANSCRIBE", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__DIARIZE", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__POLISH", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__MIND_MAP", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__INDEX", "false")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    return get_settings(reload=True)


def test_recommendation_is_deterministic_and_multilingual():
    from localplaud.template_auto import recommend_template

    meeting = recommend_template(
        title="產品同步會議", transcript="今天議程包含決議與待辦。"
    )
    assert meeting["key"] == "meeting" and meeting["confidence"] == "high"
    assert meeting == recommend_template(
        title="產品同步會議", transcript="今天議程包含決議與待辦。"
    )
    lecture = recommend_template(
        title="Machine Learning Lecture", transcript="The professor begins the lesson."
    )
    assert lecture["key"] == "lecture"
    fallback = recommend_template(title="2026-07-11", transcript="ordinary discussion")
    assert fallback["key"] == "default" and fallback["confidence"] == "low"


def test_auto_selection_api_and_pipeline_persist_actual_template(monkeypatch, tmp_path):
    settings = _reset(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient

    from localplaud.api.app import app
    from localplaud.db.models import (
        ExecutionProfile,
        FileStatus,
        NoteTemplate,
        PlaudFile,
        StageAttempt,
        StageName,
        Summary,
        Transcript,
    )
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_file

    init_db()
    audio = tmp_path / "meeting.wav"
    audio.write_bytes(b"RIFFfake")
    with session_scope() as session:
        template_profile = ExecutionProfile(
            key="meeting-derived", name="Meeting derived", version=1
        )
        session.add(template_profile)
        session.flush()
        meeting_template = session.query(NoteTemplate).filter_by(
            key="meeting", is_active=True
        ).one()
        meeting_template.execution_profile_id = template_profile.id
        session.add(
            PlaudFile(
                id="auto",
                filename="Weekly Product Sync Meeting",
                status=FileStatus.downloaded,
                audio_path=str(audio),
                note_template_key="auto",
            )
        )
        session.add(
            Transcript(
                file_id="auto",
                provider="seed",
                source="local",
                text="Agenda, decision, and action item owners.",
                segments=[
                    {
                        "text": "Agenda, decision, and action item owners.",
                        "start": 0.0,
                        "end": 2.0,
                    }
                ],
            )
        )
    client = TestClient(app)
    preview = client.get("/api/files/auto/note-template/recommendation")
    assert preview.status_code == 200
    assert preview.json()["key"] == "meeting"
    assert preview.json()["template"]["version"] == 1
    assert client.put("/api/files/auto/note-template", json={"key": "auto"}).status_code == 200

    def fake_summary(transcript, resolved):
        assert resolved.pipeline.summary_template == "meeting"
        return {
            "template": "meeting",
            "title": "Product sync",
            "content_md": "# Product sync",
            "provider": "fake",
            "model": "fake",
        }

    monkeypatch.setattr("localplaud.worker.pipeline.summarize.summarize", fake_summary)
    process_file("auto", settings=settings)
    with session_scope() as session:
        summary = session.query(Summary).filter_by(file_id="auto").one()
        stage = next(
            item for item in session.get(PlaudFile, "auto").stage_runs
            if item.stage == StageName.summarize
        )
        assert summary.template == "meeting"
        assert stage.detail["auto_template"]["key"] == "meeting"
        assert stage.detail["auto_template"]["engine"] == "local-deterministic-v1"
        upstream = next(
            item for item in session.get(PlaudFile, "auto").stage_runs
            if item.stage == StageName.transcribe
        )
        downstream = next(
            item for item in session.get(PlaudFile, "auto").stage_runs
            if item.stage == StageName.index
        )
        assert not any(
            item["kind"] == "template"
            for item in upstream.resolved_profile_snapshot["layer_provenance"]
        )
        for snapshot in (
            stage.resolved_profile_snapshot,
            downstream.resolved_profile_snapshot,
            summary.resolved_profile_snapshot,
        ):
            assert any(
                item["kind"] == "template" and item["template_key"] == "meeting"
                for item in snapshot["layer_provenance"]
            )
        attempt = session.query(StageAttempt).filter_by(
            file_id="auto", stage=StageName.summarize
        ).one()
        assert attempt.resolved_profile_snapshot["layers"] == summary.resolved_profile_snapshot[
            "layers"
        ]
