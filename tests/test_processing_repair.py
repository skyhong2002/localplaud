import json

import pytest

from localplaud.processing_repair import recovery_action, save_manifest


@pytest.fixture(autouse=True)
def isolated_priority_check(monkeypatch):
    # Queue contract tests must never read the operator's production queue.
    monkeypatch.setattr("localplaud.processing_repair.priority_work_pending", lambda _: False)


def state(**kw):
    return {"status": "partial", "active": False, "stages": {"summarize": "failed"}, **kw}


def test_quota_defers_text_without_faking_completion():
    assert recovery_action({}, state(), False) == "waiting_for_provider"
    assert recovery_action({}, state(), True) == "resume"
    assert recovery_action({}, state(active=True), False) == "running"
    assert recovery_action({}, state(status="done"), False) == "completed"


def test_finished_recording_with_stale_notes_regenerates_them():
    stale = state(status="done", derived_stale=True, stages={"summarize": "pending"})
    assert recovery_action({}, stale, False) == "waiting_for_provider"
    assert recovery_action({}, stale, True) == "resume"


def test_one_speech_retry_can_run_while_text_is_paused():
    failed = state(stages={"transcribe": "failed"})
    assert recovery_action({}, failed, False) == "resume"
    assert recovery_action({"speech_attempted": True}, failed, False) == "waiting_for_provider"
    assert recovery_action({"attempts": 3}, failed, True) == "needs_attention"
    assert recovery_action({}, {"missing": True}, True) == "skipped"
    assert recovery_action({}, state(trash=True), True) == "skipped"


def test_explicit_model_migration_does_not_reuse_old_asr():
    from localplaud.processing_repair import needs_model_upgrade

    item = {"target_asr_model": "new-model"}
    old = state(status="done", transcribe_model="old-model")
    assert needs_model_upgrade(item, old)
    assert recovery_action(item, old, False) == "resume"
    assert not needs_model_upgrade(item, state(transcribe_model="new-model"))
    assert not needs_model_upgrade(item, state())


def test_manifest_is_atomic_and_private(tmp_path):
    path = tmp_path / "private" / "queue.json"
    save_manifest(path, {"recordings": [{"id": "one", "outcome": "pending"}]})
    assert json.loads(path.read_text())["recordings"][0]["id"] == "one"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not path.with_suffix(".tmp").exists()


def test_queue_waits_for_quota_then_resumes_exact_recording(monkeypatch, tmp_path):
    import httpx
    import respx

    from localplaud import processing_repair as repair

    path = tmp_path / "queue.json"
    save_manifest(path, {"recordings": [{"id": "one", "outcome": "pending"}]})
    turns = []
    monkeypatch.setattr(repair, "text_provider_health", lambda *_: (bool(turns), "quota"))
    monkeypatch.setattr(
        repair, "recording_state", lambda _: state(status="done") if len(turns) > 1 else state()
    )
    monkeypatch.setattr(repair.time, "sleep", lambda seconds: turns.append(seconds))
    with respx.mock:
        route = respx.post("http://127.0.0.1:8080/file/one/reprocess").mock(
            return_value=httpx.Response(200)
        )
        repair.run_repair_queue(path)
        assert route.call_count == 1
        assert not route.calls[0].request.url.query
    result = json.loads(path.read_text())
    assert result["recordings"][0]["outcome"] == "completed"
    assert turns == [60, 10]


def test_queue_migrates_model_once_then_resumes_without_force(monkeypatch, tmp_path):
    import httpx
    import respx

    from localplaud import processing_repair as repair

    path = tmp_path / "queue.json"
    save_manifest(path, {"recordings": [{"id": "one", "target_asr_model": "new"}]})
    turns = []
    monkeypatch.setattr(repair, "text_provider_health", lambda *_: (True, ""))
    monkeypatch.setattr(
        repair,
        "recording_state",
        lambda _: state(
            status="done" if len(turns) > 1 else "partial",
            transcribe_model="new" if turns else "old",
        ),
    )
    monkeypatch.setattr(repair.time, "sleep", lambda seconds: turns.append(seconds))
    with respx.mock:
        route = respx.post("http://127.0.0.1:8080/file/one/reprocess").mock(
            return_value=httpx.Response(200)
        )
        repair.run_repair_queue(path)
        assert route.call_count == 2
        assert route.calls[0].request.url.params["force"] == "true"
        assert not route.calls[1].request.url.query


def test_queue_does_not_run_a_changed_profile(monkeypatch, tmp_path):
    from localplaud import processing_repair as repair

    path = tmp_path / "queue.json"
    save_manifest(
        path, {"recordings": [{"id": "one"}], "required_stages": {"transcribe": {"model": "new"}}}
    )
    monkeypatch.setattr(repair, "text_provider_health", lambda *_: (True, ""))
    monkeypatch.setattr(repair, "recording_state", lambda _: state())
    monkeypatch.setattr(repair, "profile_matches", lambda *_: False)
    repair.run_repair_queue(path)
    assert json.loads(path.read_text())["recordings"][0]["outcome"] == "needs_attention"


def test_completed_speech_recovery_only_requests_derived_artifacts():
    from localplaud.processing_repair import recovery_endpoint

    item = {"id": "one"}
    completed = state(
        has_local_transcript=True,
        stages={
            "transcribe": "completed",
            "align": "completed",
            "diarize": "completed",
            "summarize": "failed",
        },
    )
    assert recovery_endpoint(item, completed) == "/file/one/generate-notes"
    completed["stages"]["diarize"] = "degraded"
    assert recovery_endpoint(item, completed) == "/file/one/reprocess"
    completed["stages"]["diarize"] = "completed"
    completed["has_local_transcript"] = False
    assert recovery_endpoint(item, completed) == "/file/one/reprocess"
    completed["transcribe_model"] = "old"
    assert recovery_endpoint({**item, "target_asr_model": "new"}, completed).endswith("?force=true")


def test_repair_queue_yields_before_submission_without_spending_attempt(monkeypatch, tmp_path):
    from localplaud import processing_repair as repair

    path = tmp_path / "queue.json"
    save_manifest(path, {"recordings": [{"id": "one"}]})
    turns = []
    monkeypatch.setattr(
        repair, "recording_state", lambda _: state(status="done") if turns else state()
    )
    monkeypatch.setattr(repair, "text_provider_health", lambda *_: (True, ""))
    monkeypatch.setattr(repair, "priority_work_pending", lambda _: True)
    monkeypatch.setattr(repair.time, "sleep", lambda seconds: turns.append(seconds))
    repair.run_repair_queue(path)
    item = json.loads(path.read_text())["recordings"][0]
    assert item.get("attempts", 0) == 0
    assert turns == [60]
