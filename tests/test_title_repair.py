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
    assert item["prompt_version"] == "recording-title/v3"
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
