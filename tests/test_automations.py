"""Executable, idempotent local AutoFlow rules and Web/API surfaces."""

from __future__ import annotations


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'auto.db'}")
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
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
    assert client.post(
        "/api/automations/rules",
        json={
            "name": "Duplicate export",
            "trigger": {},
            "actions": {"export_formats": ["txt", "txt"]},
        },
    ).status_code == 422
    page = client.get("/discover")
    assert page.status_code == 200
    assert "AutoFlow" in page.text and "Run history" in page.text
    assert 'id="rule-form"' in page.text and "Run now" in page.text
    assert 'href="/discover"' in page.text
    assert "Create a local inbox notification" in page.text
    assert 'name="webhook_integration_id"' in page.text


def test_external_rules_are_idempotent_executable_and_read_only(monkeypatch, tmp_path):
    client, folder_id, _tag_id = _seed(monkeypatch, tmp_path)
    body = {
        "owner_key": "notion-sync",
        "owner_label": "Notion Sync",
        "external_id": "rule-42",
        "management_hint": "Edit this rule in Notion Sync.",
        "name": "External meeting filing",
        "enabled": True,
        "priority": 30,
        "trigger": {"origin": "plaud", "title_contains": "sync"},
        "actions": {"folder_id": folder_id},
        "notify": False,
    }
    created = client.put("/api/automations/external-rules", json=body)
    assert created.status_code == 200
    assert created.json()["created"] is True
    rule = created.json()["rule"]
    assert rule["editable"] is False
    assert rule["owner_type"] == "external"
    assert rule["owner_label"] == "Notion Sync"
    assert rule["version"] == 1

    unchanged = client.put("/api/automations/external-rules", json=body).json()
    assert unchanged["created"] is False
    assert unchanged["rule"]["version"] == 1
    body["name"] = "External meeting archive"
    changed = client.put("/api/automations/external-rules", json=body).json()["rule"]
    assert changed["version"] == 2

    local_body = {
        "name": "Take over",
        "enabled": True,
        "priority": 1,
        "trigger": {},
        "actions": {"folder_id": folder_id},
    }
    for response in (
        client.put(f"/api/automations/rules/{rule['id']}", json=local_body),
        client.post(f"/api/automations/rules/{rule['id']}/toggle"),
        client.delete(f"/api/automations/rules/{rule['id']}"),
    ):
        assert response.status_code == 409
        assert "managed by Notion Sync" in response.json()["detail"]
    assert client.post(f"/api/automations/rules/{rule['id']}/dry-run").status_code == 200

    page = client.get("/discover")
    assert page.status_code == 200
    assert "Applications &amp; integrations" in page.text
    assert "External rule owners" in page.text
    assert "Notion Sync · read-only" in page.text
    assert "Edit this rule in Notion Sync." in page.text
    assert f'class="btn sec rule-edit" data-id="{rule["id"]}"' not in page.text
    assert 'title="Managed by Notion Sync">Read-only</span>' in page.text

    assert client.post("/api/automations/run").json()["recordings_changed"] == 1
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        assert session.get(PlaudFile, "match").folder_id == folder_id


def test_automation_ownership_migration_is_idempotent(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from localplaud.db.migrations import migrate_automation_ownership_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-auto.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE automation_rules ("
                "id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL, "
                "enabled BOOLEAN NOT NULL, priority INTEGER NOT NULL, version INTEGER NOT NULL, "
                "trigger JSON NOT NULL, actions JSON NOT NULL, notify BOOLEAN NOT NULL, "
                "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
            )
        )
    migrated = migrate_automation_ownership_schema(engine)
    assert set(migrated) == {
        "automation_rules.owner_type",
        "automation_rules.owner_key",
        "automation_rules.owner_label",
        "automation_rules.external_id",
        "automation_rules.owner_detail",
    }
    assert migrate_automation_ownership_schema(engine) == []
    columns = {column["name"] for column in inspect(engine).get_columns("automation_rules")}
    assert {"owner_type", "owner_key", "owner_label", "external_id", "owner_detail"} <= columns


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


def test_autoflow_transcript_exports_are_durable_downloadable_and_idempotent(
    monkeypatch, tmp_path
):
    client, _folder_id, _tag_id = _seed(monkeypatch, tmp_path)
    from localplaud.db.models import AutomationExport, PlaudFile, Transcript
    from localplaud.db.session import session_scope

    with session_scope() as session:
        file = session.get(PlaudFile, "match")
        file.transcript = Transcript(
            provider="test-asr",
            source="local",
            text="hello world",
            segments=[
                {
                    "text": "hello world",
                    "start": 1.25,
                    "end": 2.5,
                    "speaker": "SPEAKER_00",
                }
            ],
        )

    rule = client.post(
        "/api/automations/rules",
        json={
            "name": "Export transcript",
            "trigger": {"origin": "plaud"},
            "actions": {"export_formats": ["txt", "srt", "vtt"]},
        },
    )
    assert rule.status_code == 201
    rule_id = rule.json()["id"]
    assert "export TXT/SRT/VTT" in rule.json()["sentence"]
    assert client.post("/api/automations/run").json()["recordings_changed"] == 1

    runs = client.get("/api/automations/runs").json()["runs"]
    assert [item["format"] for item in runs[0]["exports"]] == ["srt", "txt", "vtt"]
    assert {item["status"] for item in runs[0]["exports"]} == {"completed"}
    txt = next(item for item in runs[0]["exports"] if item["format"] == "txt")
    assert txt["provenance"]["transcript_source"] == "local"
    response = client.get(f"/api/automations/exports/{txt['id']}/download")
    assert response.status_code == 200
    assert b"hello world" in response.content
    assert 'filename="transcript.txt"' in response.headers["content-disposition"]

    with session_scope() as session:
        rows = session.query(AutomationExport).all()
        assert len(rows) == 3
        txt_path = next(row.path for row in rows if row.format == "txt")
    assert client.post("/api/automations/run").json()["recordings_changed"] == 0
    with session_scope() as session:
        assert session.query(AutomationExport).count() == 3

    from pathlib import Path

    Path(txt_path).write_text("corrupt", encoding="utf-8")
    assert client.get(f"/api/automations/exports/{txt['id']}/download").status_code == 409
    retried = client.post(f"/api/automations/exports/{txt['id']}/retry").json()
    assert retried["status"] == "completed"
    assert b"hello world" in client.get(
        f"/api/automations/exports/{txt['id']}/download"
    ).content
    assert client.delete(f"/api/automations/rules/{rule_id}").status_code == 200
    preserved = client.get(f"/api/automations/exports/{txt['id']}/download")
    assert preserved.status_code == 200
    with session_scope() as session:
        assert session.get(AutomationExport, txt["id"]).automation_run_id is None


def test_export_failure_isolated_then_retries_after_transcript_exists(
    monkeypatch, tmp_path
):
    client, folder_id, _tag_id = _seed(monkeypatch, tmp_path)
    client.post(
        "/api/automations/rules",
        json={
            "name": "Export when ready",
            "trigger": {"origin": "plaud"},
            "actions": {"folder_id": folder_id, "export_formats": ["srt"]},
        },
    )
    assert client.post("/api/automations/run").json()["recordings_changed"] == 1

    from localplaud.db.models import AutomationExport, AutomationRun, PlaudFile, Transcript
    from localplaud.db.session import session_scope

    with session_scope() as session:
        run = session.query(AutomationRun).one()
        delivery = session.query(AutomationExport).one()
        assert run.status == "completed"
        assert delivery.status == "failed"
        assert "no exportable transcript" in delivery.error
        assert session.get(PlaudFile, "match").folder_id == folder_id
        export_id = delivery.id
        session.get(PlaudFile, "match").transcript = Transcript(
            provider="test-asr",
            source="local",
            text="now ready",
            segments=[{"text": "now ready", "start": 0, "end": 1}],
        )

    retried = client.post(f"/api/automations/exports/{export_id}/retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "completed"
