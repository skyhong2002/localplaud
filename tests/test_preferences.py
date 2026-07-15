from __future__ import annotations

import re
from pathlib import Path


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
            "theme": "light",
            "density": "comfortable",
            "timezone": "Asia/Taipei",
            "hour_cycle": "24",
            "locale": "en",
            "auto_process_new_recordings": True,
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
                "theme": "light",
                "density": "compact",
                "timezone": "UTC",
                "hour_cycle": "12",
                "locale": "zh-Hant-TW",
                "auto_process_new_recordings": False,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["workspace_name"] == "Sky Lab"

        page = client.get("/settings")
        assert page.status_code == 200
        assert '<html lang="zh-Hant-TW" data-density="compact" data-theme="light">' in page.text
        assert 'name="theme" value="light"' in page.text
        assert 'option value="dark"' not in page.text
        assert "Sky Lab · 自架服務" in page.text
        assert 'id="workspace-preferences"' in page.text
        assert 'href="#workspace-preferences"' in page.text
        assert 'value="UTC"' in page.text
        assert '<option value="12" selected>12-hour</option>' in page.text
        assert '<option value="zh-Hant-TW" selected>繁體中文（台灣）</option>' in page.text
        assert 'name="auto_process_new_recordings"' in page.text
        assert 'name="auto_process_new_recordings" checked' not in page.text
        assert "自動處理新錄音" in page.text
        assert "工作區偏好設定" in page.text
        for text in (
            "帳號",
            "存取與安全性",
            "資料與備份",
            "私人工作區備份",
            "此主機的建議設定",
            "連線",
            "自訂詞彙表",
            "模型目錄",
            "執行設定檔",
            "筆記範本",
            "建立供應商連線",
            "新增模型",
            "遠端工作節點",
            "已授權的 Webhook",
            "已授權的電子郵件",
            "支援與關於",
        ):
            assert text in page.text
        assert "#workspace-preferences-form{grid-template-columns:1fr!important}" in page.text


def test_daemon_processing_respects_workspace_preference(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    calls: list[int] = []

    def fake_process_pending(_settings, *, limit):
        calls.append(limit)
        return 1

    monkeypatch.setattr("localplaud.worker.pipeline.process_pending", fake_process_pending)
    from localplaud.cli import process_automatic_pending
    from localplaud.config import get_settings

    with client:
        assert process_automatic_pending(get_settings()) == 1
        preferences = client.get("/api/preferences/workspace").json()
        response = client.put(
            "/api/preferences/workspace",
            json=preferences | {"auto_process_new_recordings": False},
        )
        assert response.status_code == 200
        assert process_automatic_pending(get_settings()) == 0
    assert calls == [1]


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
                ("/", "名稱"),
            ("/search", "即使沒有 AI 供應商"),
            ("/notes", "目前沒有已儲存筆記"),
            ("/status", "執行環境"),
        ):
            page = client.get(path)
            assert page.status_code == 200
            assert '<html lang="zh-Hant-TW"' in page.text
            assert "所有檔案" in page.text
            assert text in page.text

        settings = client.get("/settings")
        assert 'const tr=window.localplaudT' in settings.text
        assert 'window.localplaudT = message => ({' in settings.text
        assert "建立含資訊清單與 SHA-256 的一致性 SQLite 快照" in settings.text
        assert "目前沒有工作區備份" in settings.text
        assert "已授權的備份目的地" in settings.text
        assert "尚未授權遠端備份目的地" in settings.text
        assert "明確允許私人／區網目的地與 HTTP" in settings.text
        assert "依實際證據推薦本機 ASR 設定" in settings.text
        assert "密鑰只會以參照方式保存" in settings.text
        assert "修正姓名與專業詞彙，不變更原始 ASR" in settings.text
        assert "每個處理階段皆明確指定" in settings.text
        assert "對外資料傳送須明確啟用" in settings.text
        assert "SMTP 傳送須明確啟用" in settings.text
        assert "診斷檔只包含彙總計數" in settings.text
        assert "從 Plaud 匯入中繼資料" in settings.text
        assert "localStatus.textContent=`${tr('Importing')}" in settings.text
        assert "label.textContent=`${tr('Complete')}" in settings.text
        assert "button.textContent=tr('Uploading…')" in settings.text
        assert "out.textContent=tr('Authorizing…')" in settings.text
        assert "confirm(tr('Revoke this backup destination? Upload history is preserved.'))" in settings.text
        assert "out.textContent=tr('Creating a verified profile…')" in settings.text
        assert "out.textContent=tr('Created. Reloading…')" in settings.text
        assert "out.textContent=tr('Registered. Reloading…')" in settings.text
        assert "out.textContent=tr('Authorized. Reloading…')" in settings.text
        assert "out.textContent=tr(data.status||'error')" in settings.text

        from localplaud.i18n import catalog

        messages = catalog("zh-Hant-TW")
        assert messages["Saving…"] == "儲存中…"
        assert messages["healthy"] == "正常"
        assert messages["align"] == "時間對齊"
        assert "Speech character insertion" not in messages


def test_traditional_chinese_catalog_covers_all_static_template_messages():
    """Every explicitly translatable template literal must have a zh-TW value."""
    from localplaud.i18n import catalog

    template_dir = Path(__file__).parents[1] / "src/localplaud/api/templates"
    keys: set[str] = set()
    for template in template_dir.glob("*.html"):
        source = template.read_text(encoding="utf-8")
        keys.update(re.findall(r"\b(?:t|tr)\('([^']+)'\)", source))

    missing = sorted(keys - catalog("zh-Hant-TW").keys())
    assert missing == []


def test_dynamic_action_messages_use_translation_helper():
    """User-visible JS actions must not bypass the centralized locale catalog."""
    template_dir = Path(__file__).parents[1] / "src/localplaud/api/templates"
    literal_patterns = (
        r"(?:textContent\s*=|alert\(|confirm\(|prompt\()\s*'([^']*[A-Za-z][^']*)'",
        r"(?:textContent\s*=|alert\(|confirm\(|prompt\()\s*`([^`]*[A-Za-z][^`]*)`",
    )
    violations: list[str] = []
    technical_fragments = ("${position", "${matches.length}", "${stamp")
    for template in template_dir.glob("*.html"):
        source = template.read_text(encoding="utf-8")
        for pattern in literal_patterns:
            for message in re.findall(pattern, source):
                if "tr(" in message or message.startswith(technical_fragments):
                    continue
                violations.append(f"{template.name}: {message}")
    assert violations == []
