from __future__ import annotations


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'system.db'}")
    monkeypatch.setenv(
        "LOCALPLAUD_PLAUD__OFFICIAL__TOKENS_PATH", str(tmp_path / "private-tokens.json")
    )
    monkeypatch.setenv("LOCALPLAUD_API__AUTH_TOKEN", "diagnostics-api-token")
    monkeypatch.setenv("LOCALPLAUD_BUILD_COMMIT", "abcdef1234567890")
    monkeypatch.setenv("DIAGNOSTICS_SECRET_VALUE", "must-never-appear")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app

    client = TestClient(app)
    client.headers["x-auth-token"] = "diagnostics-api-token"
    return client


def test_about_and_diagnostics_are_truthful_aggregate_and_redacted(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    with client:
        from localplaud.db.models import (
            AutomationRule,
            EmailIntegration,
            PlaudFile,
            RemoteWorker,
            StageName,
            StageRun,
            StageStatus,
            WebhookIntegration,
        )
        from localplaud.db.session import session_scope

        with session_scope() as session:
            session.add(PlaudFile(id="secret-recording-id", filename="secret recording title"))
            session.add(
                StageRun(
                    file_id="secret-recording-id",
                    stage=StageName.transcribe,
                    status=StageStatus.failed,
                    error="private stage error text",
                )
            )
            session.add(
                AutomationRule(
                    name="private rule name",
                    trigger={},
                    actions={"export_formats": ["txt"]},
                )
            )
            session.add(
                WebhookIntegration(
                    name="private webhook",
                    url="https://secret-webhook.example/path",
                    secret_ref="env:DIAGNOSTICS_SECRET_VALUE",
                )
            )
            session.add(
                EmailIntegration(
                    name="private email",
                    smtp_host="secret-smtp.example",
                    from_address="from@example.com",
                    to_addresses=["private@example.com"],
                    password_ref="env:DIAGNOSTICS_SECRET_VALUE",
                )
            )
            session.add(
                RemoteWorker(
                    key="private-worker",
                    name="Private Worker",
                    base_url="https://secret-worker.example",
                )
            )

        about = client.get("/api/system/about")
        assert about.status_code == 200
        assert about.json()["product"] == "localplaud"
        assert about.json()["build_commit"] == "abcdef1234567890"
        assert about.json()["access"] == {
            "application_token_configured": True,
            "browser_login_configured": False,
            "reverse_proxy": "external / not observable by localplaud",
            "active_sessions": None,
            "session_detail": "localplaud Web App login is not configured",
        }

        response = client.get("/api/system/diagnostics.json")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert 'filename="localplaud-diagnostics.json"' in response.headers[
            "content-disposition"
        ]
        payload = response.json()
        assert payload["schema"] == "localplaud-safe-diagnostics/v1"
        assert payload["counts"]["recordings"] == 1
        assert payload["counts"]["recording_status"] == {"discovered": 1}
        assert payload["counts"]["stage_status"] == {"failed": 1}
        assert payload["counts"]["automation_rules"] == 1
        assert payload["counts"]["webhook_integrations"] == 1
        assert payload["counts"]["email_integrations"] == 1
        assert payload["counts"]["remote_workers"] == 1
        rendered = response.text
        for sensitive in (
            "secret-recording-id",
            "secret recording title",
            "private stage error text",
            "private rule name",
            "secret-webhook.example",
            "secret-smtp.example",
            "private@example.com",
            "secret-worker.example",
            "diagnostics-api-token",
            "must-never-appear",
            "private-tokens.json",
        ):
            assert sensitive not in rendered

        settings_page = client.get("/settings")
        assert settings_page.status_code == 200
        assert 'id="access-security"' in settings_page.text
        assert 'href="#access-security"' in settings_page.text
        assert 'id="support-about"' in settings_page.text
        assert 'href="#support-about"' in settings_page.text
        assert "Web login not configured" in settings_page.text
        assert "Active sessions" in settings_page.text and ">0</div>" in settings_page.text
        assert 'href="/api/system/diagnostics.json"' in settings_page.text
