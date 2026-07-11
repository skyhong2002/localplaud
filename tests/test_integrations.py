"""Authorized webhook integrations, scoped payloads, and durable delivery."""

from __future__ import annotations

import json

import httpx
import respx


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'hooks.db'}")
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("TEST_WEBHOOK_TOKEN", "super-secret-token")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def test_webhook_validation_scoped_delivery_failure_retry_and_deletion(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path)
    url = "http://127.0.0.1:9876/hook"
    base = {
        "name": "Local receiver",
        "url": url,
        "secret_ref": "env:TEST_WEBHOOK_TOKEN",
        "scopes": ["metadata", "transcript"],
        "enabled": True,
        "allow_private_network": True,
    }
    denied = client.post(
        "/api/integrations/webhooks",
        json=base | {"allow_private_network": False},
    )
    assert denied.status_code == 422
    assert "require HTTPS" in denied.json()["detail"]
    assert client.post(
        "/api/integrations/webhooks",
        json=base | {"url": "http://user:password@127.0.0.1/hook"},
    ).status_code == 422
    assert client.post(
        "/api/integrations/webhooks",
        json=base | {"url": "http://127.0.0.1/hook?token=inline"},
    ).status_code == 422

    created = client.post("/api/integrations/webhooks", json=base)
    assert created.status_code == 201
    integration = created.json()
    assert integration["secret_ref"] == "env:TEST_WEBHOOK_TOKEN"
    settings_page = client.get("/settings")
    assert "Authorized webhooks" in settings_page.text
    assert "Local receiver" in settings_page.text
    assert "super-secret-token" not in settings_page.text
    assert f'<option value="{integration["id"]}">Local receiver' in client.get(
        "/discover"
    ).text

    from localplaud.db.models import (
        AutomationRule,
        AutomationWebhookDelivery,
        PlaudFile,
        Transcript,
        WebhookIntegration,
    )
    from localplaud.db.session import session_scope

    with session_scope() as session:
        file = PlaudFile(id="hook-file", filename="Webhook meeting", origin="local")
        file.transcript = Transcript(
            provider="test-asr",
            source="local",
            text="ship it",
            segments=[
                {
                    "text": "ship it",
                    "start": 2.0,
                    "end": 3.0,
                    "speaker": "SPEAKER_00",
                }
            ],
        )
        session.add(file)

    responses = [
        httpx.Response(204),
        httpx.Response(202, text="accepted"),
        httpx.Response(503, text="down"),
        httpx.Response(200, text="recovered"),
    ]
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url).mock(side_effect=responses)
        health = client.post(f"/api/integrations/webhooks/{integration['id']}/test")
        assert health.json()["status"] == "healthy"

        rule_body = {
            "name": "Send meeting",
            "trigger": {"origin": "local"},
            "actions": {"webhook_integration_ids": [integration["id"]]},
        }
        rule = client.post("/api/automations/rules", json=rule_body)
        assert rule.status_code == 201
        rule_id = rule.json()["id"]
        assert "send webhooks" in rule.json()["sentence"]
        assert client.post("/api/automations/run").json()["recordings_changed"] == 1

        assert route.call_count == 2
        request = route.calls[1].request
        payload = json.loads(request.content)
        assert payload["recording"]["title"] == "Webhook meeting"
        assert payload["transcript"]["provenance"]["transcript_source"] == "local"
        assert payload["transcript"]["segments"][0]["text"] == "ship it"
        assert "notes" not in payload
        assert request.headers["authorization"] == "Bearer super-secret-token"
        delivery_key = request.headers["x-localplaud-delivery-id"]

        assert client.post("/api/automations/run").json()["recordings_changed"] == 0
        assert route.call_count == 2
        runs = client.get("/api/automations/runs").json()["runs"]
        first_delivery = runs[0]["webhooks"][0]
        assert first_delivery["status"] == "completed"
        assert first_delivery["response_status"] == 202

        updated = client.put(f"/api/automations/rules/{rule_id}", json=rule_body)
        assert updated.status_code == 200
        assert client.post("/api/automations/run").json()["recordings_changed"] == 1
        runs = client.get("/api/automations/runs").json()["runs"]
        failed = runs[0]["webhooks"][0]
        assert failed["status"] == "failed"
        assert "HTTP 503" in failed["error"]
        assert failed["response_status"] == 503
        assert failed["payload_sha256"]
        assert failed["attempt_count"] == 1
        retried = client.post(
            f"/api/automations/webhooks/{failed['id']}/retry"
        ).json()
        assert retried["status"] == "completed"
        assert route.calls[3].request.headers["x-localplaud-delivery-id"] != delivery_key
        assert route.calls[3].request.headers["x-localplaud-delivery-id"] == route.calls[2].request.headers[
            "x-localplaud-delivery-id"
        ]

    with session_scope() as session:
        deliveries = session.query(AutomationWebhookDelivery).all()
        assert len(deliveries) == 2
        assert deliveries[-1].attempt_count == 2
        assert deliveries[-1].payload_sha256
        assert "super-secret-token" not in str(deliveries[-1].integration_snapshot)
        assert "super-secret-token" not in str(deliveries[-1].__dict__)
        assert session.get(PlaudFile, "hook-file").folder_id is None

    assert client.delete(
        f"/api/integrations/webhooks/{integration['id']}"
    ).status_code == 409
    rule_body["actions"] = {"export_formats": ["txt"]}
    assert client.put(f"/api/automations/rules/{rule_id}", json=rule_body).status_code == 200
    assert client.delete(
        f"/api/integrations/webhooks/{integration['id']}"
    ).status_code == 204
    with session_scope() as session:
        assert session.get(WebhookIntegration, integration["id"]) is None
        assert all(row.integration_id is None for row in session.query(AutomationWebhookDelivery))
        assert session.get(AutomationRule, rule_id) is not None


def test_missing_webhook_secret_is_a_durable_failure_without_rolling_back(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.delenv("MISSING_WEBHOOK_TOKEN", raising=False)
    integration = client.post(
        "/api/integrations/webhooks",
        json={
            "name": "Missing secret",
            "url": "http://127.0.0.1:9877/hook",
            "secret_ref": "env:MISSING_WEBHOOK_TOKEN",
            "scopes": ["metadata"],
            "allow_private_network": True,
        },
    ).json()
    folder_id = client.post("/api/folders", json={"name": "Delivered"}).json()["id"]

    from localplaud.db.models import AutomationWebhookDelivery, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="missing-secret", filename="Secret test", origin="local"))
    client.post(
        "/api/automations/rules",
        json={
            "name": "Secret failure",
            "trigger": {"origin": "local"},
            "actions": {
                "folder_id": folder_id,
                "webhook_integration_ids": [integration["id"]],
            },
        },
    )
    assert client.post("/api/automations/run").json()["recordings_changed"] == 1
    with session_scope() as session:
        delivery = session.query(AutomationWebhookDelivery).one()
        assert delivery.status == "failed"
        assert "environment variable is missing" in delivery.error
        assert session.get(PlaudFile, "missing-secret").folder_id == folder_id


def test_disabling_webhook_after_rule_creation_skips_egress_not_core_actions(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path)
    body = {
        "name": "Disable later",
        "url": "http://127.0.0.1:9878/hook",
        "secret_ref": None,
        "scopes": ["metadata"],
        "enabled": True,
        "allow_private_network": True,
    }
    integration = client.post("/api/integrations/webhooks", json=body).json()
    folder_id = client.post("/api/folders", json={"name": "Still applied"}).json()["id"]

    from localplaud.db.models import AutomationWebhookDelivery, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="disabled-hook", filename="Disabled hook", origin="local"))
    client.post(
        "/api/automations/rules",
        json={
            "name": "Disable isolation",
            "trigger": {"origin": "local"},
            "actions": {
                "folder_id": folder_id,
                "webhook_integration_ids": [integration["id"]],
            },
        },
    )
    assert client.put(
        f"/api/integrations/webhooks/{integration['id']}",
        json=body | {"enabled": False},
    ).status_code == 200
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(body["url"]).mock(return_value=httpx.Response(200))
        assert client.post("/api/automations/run").json()["recordings_changed"] == 1
        assert route.call_count == 0
    with session_scope() as session:
        delivery = session.query(AutomationWebhookDelivery).one()
        assert delivery.status == "failed"
        assert "disabled before this run" in delivery.error
        assert session.get(PlaudFile, "disabled-hook").folder_id == folder_id
