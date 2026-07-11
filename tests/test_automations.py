"""Executable, idempotent local AutoFlow rules and Web/API surfaces."""

from __future__ import annotations


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'auto.db'}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    folder_id = client.post("/api/folders", json={"name": "Meetings"}).json()["id"]
    tag_id = client.post("/api/tags", json={"name": "Automated"}).json()["id"]
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="match",
                filename="Weekly Product Sync",
                origin="plaud",
                duration_ms=35 * 60_000,
            )
        )
        session.add(
            PlaudFile(
                id="skip",
                filename="Personal memo",
                origin="local",
                duration_ms=3 * 60_000,
            )
        )
    return client, folder_id, tag_id


def test_rule_dry_run_execution_history_and_versioning(monkeypatch, tmp_path):
    client, folder_id, tag_id = _seed(monkeypatch, tmp_path)
    body = {
        "name": "Plaud sync meetings",
        "enabled": True,
        "priority": 20,
        "trigger": {
            "origin": "plaud",
            "title_contains": "sync",
            "min_duration_minutes": 10,
        },
        "actions": {
            "note_template_key": "meeting",
            "folder_id": folder_id,
            "add_tag_ids": [tag_id],
        },
        "notify": True,
    }
    created = client.post("/api/automations/rules", json=body)
    assert created.status_code == 201
    rule = created.json()
    assert "title contains" in rule["sentence"] and "use meeting notes" in rule["sentence"]

    dry = client.post(f"/api/automations/rules/{rule['id']}/dry-run").json()
    assert dry["mutated"] is False
    assert [row["file_id"] for row in dry["matches"]] == ["match"]
    from localplaud.db.models import AutomationRun, Notification, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        assert session.get(PlaudFile, "match").folder_id is None

    ran = client.post("/api/automations/run").json()
    assert ran["recordings_changed"] == 1
    with session_scope() as session:
        row = session.get(PlaudFile, "match")
        assert row.note_template_key == "meeting" and row.folder_id == folder_id
        assert {tag.id for tag in row.tags} == {tag_id}
        assert session.query(AutomationRun).count() == 1
        notification = session.query(Notification).one()
        assert notification.file_id == "match"
        assert notification.detail["rule_name"] == "Plaud sync meetings"
        assert notification.detail["applied"]["folder_id"] == folder_id
    assert client.post("/api/automations/run").json()["recordings_changed"] == 0
    assert len(client.get("/api/automations/notifications").json()["notifications"]) == 1

    body["actions"]["note_template_key"] = "call"
    updated = client.put(f"/api/automations/rules/{rule['id']}", json=body)
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert client.post("/api/automations/run").json()["recordings_changed"] == 1
    with session_scope() as session:
        assert session.get(PlaudFile, "match").note_template_key == "call"
        assert session.query(AutomationRun).count() == 2
        assert session.query(Notification).count() == 2


def test_lower_priority_number_wins_and_toggle_stops_execution(monkeypatch, tmp_path):
    client, _folder_id, _tag_id = _seed(monkeypatch, tmp_path)
    broad = {
        "name": "Broad",
        "priority": 200,
        "trigger": {"origin": "plaud"},
        "actions": {"note_template_key": "personal"},
    }
    specific = {
        "name": "Specific",
        "priority": 10,
        "trigger": {"origin": "plaud", "title_contains": "sync"},
        "actions": {"note_template_key": "meeting"},
    }
    broad_id = client.post("/api/automations/rules", json=broad).json()["id"]
    specific_id = client.post("/api/automations/rules", json=specific).json()["id"]
    client.post("/api/automations/run")
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        assert session.get(PlaudFile, "match").note_template_key == "meeting"
    assert client.post(f"/api/automations/rules/{specific_id}/toggle").json()["enabled"] is False
    assert client.delete(f"/api/automations/rules/{broad_id}").status_code == 200


def test_rule_validation_and_discover_ui(monkeypatch, tmp_path):
    client, folder_id, _tag_id = _seed(monkeypatch, tmp_path)
    invalid = client.post(
        "/api/automations/rules",
        json={
            "name": "Bad references",
            "trigger": {},
            "actions": {"folder_id": folder_id + 999},
        },
    )
    assert invalid.status_code == 422
    assert client.post(
        "/api/automations/rules",
        json={"name": "No action", "trigger": {}, "actions": {}},
    ).status_code == 422
    page = client.get("/discover")
    assert page.status_code == 200
    assert "AutoFlow" in page.text and "Run history" in page.text
    assert 'id="rule-form"' in page.text and "Run now" in page.text
    assert 'href="/discover"' in page.text
    assert "Create a local inbox notification" in page.text


def test_notification_inbox_read_dismiss_and_rule_deletion(monkeypatch, tmp_path):
    client, _folder_id, tag_id = _seed(monkeypatch, tmp_path)
    rule_id = client.post(
        "/api/automations/rules",
        json={
            "name": "Notify me",
            "trigger": {"origin": "plaud"},
            "actions": {"add_tag_ids": [tag_id]},
            "notify": True,
        },
    ).json()["id"]
    client.post("/api/automations/run")
    item = client.get("/api/automations/notifications?unread_only=true").json()[
        "notifications"
    ][0]
    assert item["read_at"] is None
    assert "Notify me" in item["title"]
    assert "Notifications" in client.get("/notifications").text

    marked = client.post(f"/api/automations/notifications/{item['id']}/read").json()
    assert marked["read_at"] is not None
    assert client.get("/api/automations/notifications?unread_only=true").json() == {
        "notifications": []
    }
    assert client.post(
        f"/api/automations/notifications/{item['id']}/read?read=false"
    ).json()["read_at"] is None

    assert client.delete(f"/api/automations/rules/{rule_id}").status_code == 200
    preserved = client.get("/api/automations/notifications").json()["notifications"][0]
    assert preserved["automation_run_id"] is None
    assert preserved["detail"]["rule_name"] == "Notify me"
    assert client.delete(
        f"/api/automations/notifications/{item['id']}"
    ).json() == {"dismissed": True}
    assert client.get("/api/automations/notifications").json() == {"notifications": []}


def test_notification_failure_does_not_rollback_actions_and_can_retry(
    monkeypatch, tmp_path
):
    client, folder_id, _tag_id = _seed(monkeypatch, tmp_path)
    client.post(
        "/api/automations/rules",
        json={
            "name": "Isolated notification",
            "trigger": {"origin": "plaud"},
            "actions": {"folder_id": folder_id},
            "notify": True,
        },
    )
    import localplaud.automations as automations

    real_delivery = automations.deliver_local_notification
    monkeypatch.setattr(
        automations,
        "deliver_local_notification",
        lambda _run_id: (_ for _ in ()).throw(RuntimeError("inbox unavailable")),
    )
    result = client.post("/api/automations/run").json()
    assert result["recordings_changed"] == 1

    from localplaud.db.models import AutomationRun, Notification, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        run = session.query(AutomationRun).one()
        run_id = run.id
        assert run.status == "completed"
        assert run.detail["notification"]["status"] == "failed"
        assert session.get(PlaudFile, "match").folder_id == folder_id
        assert session.query(Notification).count() == 0

    monkeypatch.setattr(automations, "deliver_local_notification", real_delivery)
    retried = client.post(f"/api/automations/runs/{run_id}/retry-notification")
    assert retried.status_code == 200
    assert retried.json()["status"] == "delivered"
    assert client.post(f"/api/automations/runs/{run_id}/retry-notification").json() == retried.json()
