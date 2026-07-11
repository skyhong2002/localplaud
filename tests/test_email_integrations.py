"""Authorized SMTP destinations and durable AutoFlow email delivery."""

from __future__ import annotations


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'email.db'}")
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("TEST_SMTP_PASSWORD", "smtp-super-secret")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _fake_smtp(monkeypatch, outcomes):
    sent = []
    logins = []
    starttls = []

    class FakeSMTP:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def ehlo(self):
            return 250, b"ok"

        def starttls(self, *, context):
            starttls.append(context)
            return 220, b"ready"

        def login(self, username, password):
            logins.append((username, password))
            return 235, b"ok"

        def send_message(self, message):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            sent.append(message)
            return outcome

    monkeypatch.setattr("localplaud.email_integrations.smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("localplaud.email_integrations.smtplib.SMTP_SSL", FakeSMTP)
    return sent, logins, starttls


def test_email_validation_scoped_delivery_retry_and_deletion(monkeypatch, tmp_path):
    import smtplib

    client = _client(monkeypatch, tmp_path)
    base = {
        "name": "Team email",
        "smtp_host": "127.0.0.1",
        "smtp_port": 2525,
        "security": "starttls",
        "allow_insecure_private": True,
        "username": "smtp-user",
        "password_ref": "env:TEST_SMTP_PASSWORD",
        "from_address": "localplaud@example.com",
        "to_addresses": ["team@example.com"],
        "subject_prefix": "[Meetings]",
        "scopes": ["metadata", "transcript", "notes"],
        "enabled": True,
    }
    assert client.post(
        "/api/integrations/emails",
        json=base | {"security": "plain", "allow_insecure_private": False},
    ).status_code == 422
    assert client.post(
        "/api/integrations/emails",
        json=base | {"allow_insecure_private": False},
    ).status_code == 422
    assert client.post(
        "/api/integrations/emails",
        json=base | {"from_address": "safe@example.com\nBcc: stolen@example.com"},
    ).status_code == 422
    assert client.post(
        "/api/integrations/emails",
        json=base | {"username": None},
    ).status_code == 422

    integration = client.post("/api/integrations/emails", json=base)
    assert integration.status_code == 201
    integration = integration.json()
    settings_page = client.get("/settings")
    assert "Authorized email" in settings_page.text
    assert "Team email" in settings_page.text
    assert "smtp-super-secret" not in settings_page.text
    assert f'<option value="{integration["id"]}">Team email' in client.get("/discover").text

    from localplaud.db.models import (
        AutomationEmailDelivery,
        AutomationRule,
        EmailIntegration,
        PlaudFile,
        Summary,
        Transcript,
    )
    from localplaud.db.session import session_scope

    with session_scope() as session:
        file = PlaudFile(id="email-file", filename="Email meeting", origin="local")
        file.transcript = Transcript(
            provider="test-asr",
            source="local",
            text="approve launch",
            segments=[
                {
                    "text": "approve launch",
                    "start": 65.0,
                    "end": 66.0,
                    "speaker": "SPEAKER_00",
                }
            ],
        )
        file.summaries = [
            Summary(template="meeting", source="local", content_md="Decision: ship")
        ]
        session.add(file)

    sent, logins, starttls = _fake_smtp(
        monkeypatch,
        [{}, {}, smtplib.SMTPConnectError(421, "down"), {}],
    )
    health = client.post(f"/api/integrations/emails/{integration['id']}/test")
    assert health.json()["status"] == "healthy"
    assert "No recording data is included" in sent[0].get_content()

    folder_id = client.post("/api/folders", json={"name": "Emailed"}).json()["id"]
    rule_body = {
        "name": "Email meeting",
        "trigger": {"origin": "local"},
        "actions": {
            "folder_id": folder_id,
            "email_integration_ids": [integration["id"]],
        },
    }
    rule = client.post("/api/automations/rules", json=rule_body)
    assert rule.status_code == 201
    rule_id = rule.json()["id"]
    assert "send email" in rule.json()["sentence"]
    assert client.post("/api/automations/run").json()["recordings_changed"] == 1
    message = sent[1]
    assert message["Subject"] == "[Meetings] Email meeting"
    assert message["Message-ID"].startswith("<autoflow-run-")
    assert message["X-Localplaud-Delivery-Id"] in message["Message-ID"]
    assert "approve launch" in message.get_content()
    assert "Decision: ship" in message.get_content()
    assert logins == [("smtp-user", "smtp-super-secret")] * 2
    assert len(starttls) == 2

    assert client.post("/api/automations/run").json()["recordings_changed"] == 0
    assert len(sent) == 2
    updated = client.put(f"/api/automations/rules/{rule_id}", json=rule_body)
    assert updated.status_code == 200
    assert client.post("/api/automations/run").json()["recordings_changed"] == 1
    failed = client.get("/api/automations/runs").json()["runs"][0]["emails"][0]
    assert failed["status"] == "failed"
    assert "down" in failed["error"]
    assert failed["attempt_count"] == 1
    message_id = failed["message_id"]
    retried = client.post(f"/api/automations/emails/{failed['id']}/retry").json()
    assert retried["status"] == "completed"
    assert retried["message_id"] == message_id

    with session_scope() as session:
        deliveries = session.query(AutomationEmailDelivery).all()
        assert len(deliveries) == 2
        assert deliveries[-1].attempt_count == 2
        assert deliveries[-1].payload_sha256
        assert "smtp-super-secret" not in str(deliveries[-1].integration_snapshot)
        assert "smtp-super-secret" not in str(deliveries[-1].__dict__)
        assert session.get(PlaudFile, "email-file").folder_id == folder_id

    assert client.delete(f"/api/integrations/emails/{integration['id']}").status_code == 409
    rule_body["actions"] = {"export_formats": ["txt"]}
    assert client.put(f"/api/automations/rules/{rule_id}", json=rule_body).status_code == 200
    assert client.delete(f"/api/integrations/emails/{integration['id']}").status_code == 204
    with session_scope() as session:
        assert session.get(EmailIntegration, integration["id"]) is None
        assert all(row.integration_id is None for row in session.query(AutomationEmailDelivery))
        assert session.get(AutomationRule, rule_id) is not None


def test_disabled_email_skips_smtp_without_rolling_back(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    base = {
        "name": "Disable later",
        "smtp_host": "127.0.0.1",
        "smtp_port": 2526,
        "security": "plain",
        "allow_insecure_private": True,
        "username": None,
        "password_ref": None,
        "from_address": "localplaud@example.com",
        "to_addresses": ["team@example.com"],
        "subject_prefix": "[localplaud]",
        "scopes": ["metadata"],
        "enabled": True,
    }
    integration = client.post("/api/integrations/emails", json=base).json()
    folder_id = client.post("/api/folders", json={"name": "Still local"}).json()["id"]

    from localplaud.db.models import AutomationEmailDelivery, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="disabled-email", filename="Disabled email", origin="local"))
    client.post(
        "/api/automations/rules",
        json={
            "name": "Disabled email isolation",
            "trigger": {"origin": "local"},
            "actions": {
                "folder_id": folder_id,
                "email_integration_ids": [integration["id"]],
            },
        },
    )
    assert client.put(
        f"/api/integrations/emails/{integration['id']}",
        json=base | {"enabled": False},
    ).status_code == 200
    sent, _logins, _starttls = _fake_smtp(monkeypatch, [{}])
    assert client.post("/api/automations/run").json()["recordings_changed"] == 1
    assert sent == []
    with session_scope() as session:
        delivery = session.query(AutomationEmailDelivery).one()
        assert delivery.status == "failed"
        assert "disabled before this run" in delivery.error
        assert session.get(PlaudFile, "disabled-email").folder_id == folder_id


def test_missing_smtp_password_is_durable_and_core_actions_commit(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.delenv("MISSING_SMTP_PASSWORD", raising=False)
    integration = client.post(
        "/api/integrations/emails",
        json={
            "name": "Missing password",
            "smtp_host": "127.0.0.1",
            "smtp_port": 2527,
            "security": "plain",
            "allow_insecure_private": True,
            "username": "smtp-user",
            "password_ref": "env:MISSING_SMTP_PASSWORD",
            "from_address": "localplaud@example.com",
            "to_addresses": ["team@example.com"],
            "subject_prefix": "[localplaud]",
            "scopes": ["metadata"],
            "enabled": True,
        },
    ).json()
    folder_id = client.post("/api/folders", json={"name": "Committed"}).json()["id"]

    from localplaud.db.models import AutomationEmailDelivery, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="missing-password", filename="Missing password", origin="local"))
    client.post(
        "/api/automations/rules",
        json={
            "name": "Missing password isolation",
            "trigger": {"origin": "local"},
            "actions": {
                "folder_id": folder_id,
                "email_integration_ids": [integration["id"]],
            },
        },
    )
    sent, _logins, _starttls = _fake_smtp(monkeypatch, [{}])
    assert client.post("/api/automations/run").json()["recordings_changed"] == 1
    assert sent == []
    with session_scope() as session:
        delivery = session.query(AutomationEmailDelivery).one()
        assert delivery.status == "failed"
        assert "environment variable is missing" in delivery.error
        assert session.get(PlaudFile, "missing-password").folder_id == folder_id
