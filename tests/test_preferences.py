from __future__ import annotations


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'prefs.db'}")
    monkeypatch.setenv(
        "LOCALPLAUD_PLAUD__OFFICIAL__TOKENS_PATH", str(tmp_path / "plaud-tokens.json")
    )
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app

    return TestClient(app)


def test_workspace_preferences_are_validated_persisted_and_rendered(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    with client:
        defaults = client.get("/api/preferences/workspace")
        assert defaults.status_code == 200
        assert defaults.json() == {
            "workspace_name": "localplaud",
            "theme": "system",
            "density": "comfortable",
            "timezone": "Asia/Taipei",
            "hour_cycle": "24",
            "locale": "en",
        }

        invalid = client.put(
            "/api/preferences/workspace",
            json=defaults.json() | {"timezone": "Not/A_Timezone"},
        )
        assert invalid.status_code == 422
        assert client.get("/api/preferences/workspace").json() == defaults.json()

        updated = client.put(
            "/api/preferences/workspace",
            json={
                "workspace_name": "Sky Lab",
                "theme": "dark",
                "density": "compact",
                "timezone": "UTC",
                "hour_cycle": "12",
                "locale": "zh-Hant-TW",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["workspace_name"] == "Sky Lab"

        page = client.get("/settings")
        assert page.status_code == 200
        assert '<html lang="zh-Hant-TW" data-density="compact" data-theme="dark">' in page.text
        assert "Sky Lab · 自架服務" in page.text
        assert 'id="workspace-preferences"' in page.text
        assert 'href="#workspace-preferences"' in page.text
        assert 'value="UTC"' in page.text
        assert '<option value="12" selected>12-hour</option>' in page.text
        assert '<option value="zh-Hant-TW" selected>繁體中文（台灣）</option>' in page.text
        assert "工作區偏好設定" in page.text
        assert "#workspace-preferences-form{grid-template-columns:1fr!important}" in page.text


def test_workspace_timezone_and_clock_apply_to_recorded_dates(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    with client:
        body = client.get("/api/preferences/workspace").json() | {
            "timezone": "UTC",
            "hour_cycle": "12",
        }
        assert client.put("/api/preferences/workspace", json=body).status_code == 200

        from localplaud.db.models import FileStatus, PlaudFile
        from localplaud.db.session import session_scope

        with session_scope() as session:
            session.add(
                PlaudFile(
                    id="dated",
                    filename="Dated recording",
                    status=FileStatus.discovered,
                    start_time_ms=3_600_000,
                    duration_ms=60_000,
                )
            )
        page = client.get("/home")
        assert page.status_code == 200
        assert "Dated recording" in page.text
        assert "AM" in page.text or "PM" in page.text


def test_interface_locale_translates_shell_and_primary_pages(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    with client:
        defaults = client.get("/api/preferences/workspace").json()
        invalid = client.put(
            "/api/preferences/workspace", json=defaults | {"locale": "not-a-locale"}
        )
        assert invalid.status_code == 422

        updated = client.put(
            "/api/preferences/workspace", json=defaults | {"locale": "zh-Hant-TW"}
        )
        assert updated.status_code == 200
        for path, text in (
            ("/home", "歡迎回來"),
            ("/templates", "結構化筆記"),
            ("/discover", "本機自動化"),
            ("/notifications", "目前沒有通知"),
        ):
            page = client.get(path)
            assert page.status_code == 200
            assert '<html lang="zh-Hant-TW"' in page.text
            assert "所有檔案" in page.text
            assert text in page.text
