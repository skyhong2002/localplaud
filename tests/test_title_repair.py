"""Title-only repair preserves all other artifacts and rejects stale plans."""

import pytest

from localplaud.config import get_settings
from localplaud.db.models import PlaudFile, Summary, Transcript
from localplaud.db.session import session_scope
from localplaud.title_repair import apply_repair, plan_repair


@pytest.fixture
def recording(monkeypatch, tmp_path):
    import localplaud.db.session as db_session

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'titles.db'}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    db_session.init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="rec", filename="Cloud name", generated_title="Autopilot 模板總結"))
        session.add(Transcript(file_id="rec", source="local", provider="asr", text="週五部署新版",
                               segments=[{"text": "週五部署新版", "start": 0, "end": 1}]))
        session.add(Summary(file_id="rec", source="local", template="plaud-autopilot",
                            title="Autopilot 模板總結", content_md="Original note",
                            llm_provider="ollama", model="test-model",
                            resolved_profile_snapshot={"stages": {"summarize": {
                                "provider_type": "ollama", "model": "test-model"}}}))
    monkeypatch.setattr("localplaud.title_repair.summarize.generate_recording_title",
                        lambda *_: "新版部署：週五上線安排")
    return get_settings()


def test_plan_is_read_only_apply_is_idempotent_and_preserves_artifacts(recording):
    item = plan_repair("rec", recording)
    with session_scope() as session:
        assert session.get(PlaudFile, "rec").generated_title == "Autopilot 模板總結"
    assert item["prompt_version"] == "recording-title/v4"
    assert item["model"] == "test-model"
    assert apply_repair(item, recording)
    assert not apply_repair(item, recording)
    with session_scope() as session:
        row = session.get(PlaudFile, "rec")
        assert row.display_title == "新版部署：週五上線安排"
        assert row.filename == "Cloud name"
        assert row.generated_title_provider == "ollama"
        assert row.generated_title_model == "test-model"
        assert row.summaries[0].content_md == "Original note"
        assert row.transcripts[0].text == "週五部署新版"


@pytest.mark.parametrize("change", ["manual", "transcript", "processing", "title"])
def test_apply_rejects_concurrent_changes(recording, change):
    item = plan_repair("rec", recording)
    with session_scope() as session:
        row = session.get(PlaudFile, "rec")
        if change == "manual":
            row.local_title = "My title"
        elif change == "transcript":
            row.transcripts[0].segments = [{"text": "Changed", "start": 0, "end": 1}]
        elif change == "processing":
            row.processing_token = "active"
        else:
            row.generated_title = "New generation"
    assert not apply_repair(item, recording)


def test_cloud_transcript_cannot_supply_repair(recording):
    with session_scope() as session:
        session.get(PlaudFile, "rec").transcripts[0].source = "plaud"
    with pytest.raises(ValueError, match="canonical local transcript"):
        plan_repair("rec", recording)


def test_invalid_response_does_not_replace_title(recording, monkeypatch):
    monkeypatch.setattr("localplaud.title_repair.summarize.generate_recording_title",
                        lambda *_: "Autopilot 模板使用示例")
    with pytest.raises(ValueError, match="invalid recording title"):
        plan_repair("rec", recording)
    with session_scope() as session:
        assert session.get(PlaudFile, "rec").generated_title == "Autopilot 模板總結"


def test_reviewed_insufficient_evidence_restores_source_name(recording):
    item = plan_repair("rec", recording)
    item.update(title=None, action="restore-source-name",
                review_reason="Transcript contains only repeated ASR fragments")
    assert apply_repair(item, recording)
    assert not apply_repair(item, recording)
    with session_scope() as session:
        row = session.get(PlaudFile, "rec")
        assert row.display_title == "Cloud name"
        assert row.generated_title is None
        assert row.generated_title_provider is None
        assert row.generated_title_at is None
        assert row.summaries[0].content_md == "Original note"


def test_prior_title_repair_can_use_primary_note_provenance(recording):
    with session_scope() as session:
        session.get(PlaudFile, "rec").generated_title = "Earlier title-only repair"
    assert plan_repair("rec", recording)["model"] == "test-model"


def test_date_filter_normalizes_naive_and_aware_values():
    from datetime import UTC, datetime

    from localplaud.title_repair import _utc

    assert _utc(datetime(2026, 9, 19)) == datetime(2026, 9, 19, tzinfo=UTC)


@pytest.mark.parametrize("fail", [False, True])
def test_remote_title_retry_preserves_notes_even_when_it_fails(recording, monkeypatch, fail):
    from localplaud.worker import pipeline
    from localplaud.worker.title_policy import TITLE_PROMPT_VERSION

    recording.pipeline.mind_map = False
    recording.pipeline.index = False
    snapshot = {"stages": {"summarize": {"execution_target": "remote_worker"}}}
    calls = []

    def remote(*args, options, **kwargs):
        calls.append(options)
        if options.get("title_only"):
            if fail:
                raise TimeoutError("title unavailable")
            return {"title": "新版發布時程", "title_prompt_version": TITLE_PROMPT_VERSION}
        return {"title": "Key Points", "content_md": "## Summary\n週五發布",
                "template": "plaud-autopilot", "provider": "ollama", "model": "test-model"}

    monkeypatch.setattr(pipeline, "_run_remote_stage", remote)
    monkeypatch.setattr(pipeline, "_settings_for_stage", lambda *_: recording.model_copy(deep=True))
    monkeypatch.setattr(pipeline, "_cost_guard", lambda *_: {})
    monkeypatch.setattr(pipeline, "_run_fallback_stage",
                        lambda fid, stage, name, snap, fn: (fn(snap)["value"], snap))
    monkeypatch.setattr("localplaud.worker.knowledge_index.process_file_documents", lambda *_: None)
    transcript, _ = pipeline._load_transcript("rec", recording)
    from localplaud.worker.claims import processing_claim

    token = pipeline.claim_processing_work("rec", require_audio=False)
    with processing_claim("rec", token):
        errors = pipeline._run_derived_stages("rec", recording, transcript, "plaud-autopilot",
                                             snapshot, force=True)
    assert bool(errors) == fail
    assert len(calls) == 2
    assert calls[1]["title_only"] is True
    with session_scope() as session:
        row = session.get(PlaudFile, "rec")
        assert row.summaries[0].content_md == "## Summary\n週五發布"
        if not fail:
            assert row.generated_title == "新版發布時程"
