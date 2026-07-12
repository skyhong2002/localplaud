"""Tests for the web UI pages render (dashboard, search, status, detail)."""

from __future__ import annotations

import hashlib


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'ui.db'}")
    monkeypatch.setenv("LOCALPLAUD_PLAUD__PROVIDER", "official")
    monkeypatch.setenv(
        "LOCALPLAUD_PLAUD__OFFICIAL__TOKENS_PATH", str(tmp_path / "plaud-tokens.json")
    )
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed(audio_path: str | None = None):
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Summary,
        Transcript,
    )
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r1", filename="Weekly Sync", status=FileStatus.done,
                        duration_ms=600000, start_time_ms=1783582737000, scene=1,
                        audio_path=audio_path))
        s.add(Transcript(file_id="r1", provider="faster-whisper", language="en", has_speakers=True,
                         text="hi", segments=[{"text": "hello team", "start": 1.0, "end": 2.0, "speaker": "SPEAKER_00"}]))
        s.add(Summary(file_id="r1", template="meeting", title="Sync", content_md="# Sync\n\n- point"))
        s.add(
            StageRun(
                file_id="r1",
                stage=StageName.index,
                status=StageStatus.failed,
                attempts=1,
                error="embedding model unavailable",
            )
        )
        s.add(
            StageRun(
                file_id="r1",
                stage=StageName.correct,
                status=StageStatus.completed,
                attempts=1,
                provider="opencode-go",
                model="qwen3.7-plus",
                detail={
                    "strategy": "contextual-segment-map",
                    "revision": 2,
                    "segments": 107,
                    "chunks": 3,
                    "prompt_version": "transcript-polish/v1",
                },
            )
        )
        s.add(
            StageRun(
                file_id="r1",
                stage=StageName.align,
                status=StageStatus.completed,
                attempts=1,
                provider="faster-whisper",
                detail={
                    "strategy": "provider-word-timestamps",
                    "forced_alignment": False,
                    "word_count": 42,
                },
            )
        )


def test_dashboard_renders(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/")
    assert r.status_code == 200
    assert "Weekly Sync" in r.text
    assert "Total audio" in r.text  # stat tiles present


def test_import_dialog_is_hidden_until_explicitly_opened(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    response = c.get("/")
    assert response.status_code == 200
    assert '[hidden] { display:none !important; }' in response.text
    assert 'id="import-backdrop" hidden' in response.text
    assert "document.getElementById('import-close').addEventListener('click'" in response.text


def test_product_pages_use_centered_responsive_layout(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    for path in ("/home", "/templates", "/discover", "/notifications"):
        response = c.get(path)
        assert response.status_code == 200
        assert '<main class="main"><div class="content"' in response.text
        assert ".main { flex:1; min-width:0; overflow-y:auto; }" in response.text
        assert "width:100%; margin:0 auto; padding:26px 30px 80px" in response.text
        assert ".main > .content{ padding:20px 16px 60px; }" in response.text


def test_browser_runtime_is_vendored_and_checksum_pinned(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    page = c.get("/")
    assert '<script src="/static/htmx-1.9.12.min.js"></script>' in page.text
    assert "unpkg.com" not in page.text
    runtime = c.get("/static/htmx-1.9.12.min.js")
    assert runtime.status_code == 200
    assert hashlib.sha256(runtime.content).hexdigest() == (
        "449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452"
    )
    license_response = c.get("/static/HTMX-LICENSE.txt")
    assert license_response.status_code == 200
    assert b"Zero-Clause BSD" in license_response.content


def test_home_renders_recent_recordings_and_operational_cards(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import ImportRun
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            ImportRun(
                id="home-import",
                source="plaud",
                status="completed",
                total=754,
                processed=754,
                transcript_count=280,
                summary_count=281,
            )
        )
    response = c.get("/home")
    assert response.status_code == 200
    assert "Welcome back" in response.text
    assert "Recent recordings" in response.text and "Weekly Sync" in response.text
    assert "Plaud mirror" in response.text and "754 / 754" in response.text
    assert "AutoFlow activity" in response.text
    assert 'id="home-import-plaud"' in response.text
    assert 'href="/home"' in response.text and 'href="/"' in response.text


def test_detail_page_renders(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    _seed(str(audio))
    r = c.get("/file/r1")
    assert r.status_code == 200
    assert "SPEAKER_00" in r.text
    assert 'data-start' in r.text  # seekable segments
    assert "meeting" in r.text.lower()  # summary tab
    assert "Processing details" in r.text
    assert "Provider word timestamps validated" in r.text
    assert "42 timed words" in r.text
    assert "Forced alignment was not used" in r.text
    assert "AI-polished transcript" in r.text
    assert "revision 2" in r.text
    assert "107 segments / 3 chunks" in r.text
    assert "prompt transcript-polish/v1" in r.text
    assert "embedding model unavailable" in r.text
    assert "Resume" in r.text and "Rebuild all" in r.text
    assert "Execution profile" in r.text and "Current Settings" in r.text
    assert "Find in transcript" in r.text and "Replace all" in r.text
    assert 'id="persistent-player"' in r.text and 'id="waveform"' in r.text
    assert 'id="subscription-independence"' in r.text
    assert "Subscription independence" in r.text
    assert 'data-summary-copy=' in r.text
    assert 'id="benchmark-backdrop"' not in r.text
    assert 'id="open-benchmark"' not in r.text
    evidence = c.get("/api/files/r1/acceptance")
    assert evidence.status_code == 200
    assert evidence.json()["schema"] == "localplaud-subscription-independence/v1"
    assert {item["name"] for item in evidence.json()["checks"]} >= {
        "local_transcript",
        "transcript_polish",
        "word_alignment",
        "speaker_diarization",
        "local_notes",
        "ask_index",
        "required_exports",
    }


def test_detail_workspace_uses_traditional_chinese_locale(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    _seed(str(audio))
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        recording = session.get(PlaudFile, "r1")
        recording.status = FileStatus.error
        recording.error = "provider unavailable"
        recording.pipeline_retry_count = 99
    with c:
        preferences = c.get("/api/preferences/workspace").json()
        assert c.put(
            "/api/preferences/workspace",
            json=preferences | {"locale": "zh-Hant-TW"},
        ).status_code == 200
        page = c.get("/file/r1")
    assert page.status_code == 200
    assert '<html lang="zh-Hant-TW"' in page.text
    assert "const tr=window.localplaudT" in page.text
    assert "output.textContent=tr('Removing local data…')" in page.text
    assert "output.textContent=tr('Replacing…')" in page.text
    assert "button.textContent=tr('Importing…')" in page.text
    assert "out.textContent=tr('Checking recording signals…')" in page.text
    for text in (
        "儲存標題",
        "繼續處理",
        "全部重建",
        "本機資料",
        "執行設定檔",
        "筆記範本",
        "處理詳情",
        "建立索引",
        "已驗證供應商提供的逐字時間戳記",
        "42 個具時間戳記的詞",
        "未使用 forced alignment",
        "AI 潤飾逐字稿",
        "修訂 2",
        "107 個段落 / 3 個區塊",
        "提示詞 transcript-polish/v1",
        "失敗",
        "逐字稿",
        "在逐字稿中尋找",
        "匯出錄音",
        "時間戳記",
        "訂閱獨立性",
        "尚未通過",
        "JSON 證據",
        "自動重試次數已用盡",
        "按下繼續處理會立即重試並重設次數",
    ):
        assert text in page.text


def test_metadata_only_plaud_recording_offers_audio_import(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/file/r1")
    assert r.status_code == 200
    assert "Import audio" in r.text
    assert 'hx-post="/api/files/r1/reprocess"' not in r.text
    assert 'hx-post="/api/files/r1/reprocess?force=true"' not in r.text


def test_recording_profile_picker_persists_override(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import ExecutionProfile, RecordingProfileOverride
    from localplaud.db.session import session_scope

    with session_scope() as session:
        profile_id = session.query(ExecutionProfile.id).filter_by(is_system_default=True).scalar()
    response = c.post("/file/r1/profile", data={"profile_id": profile_id}, follow_redirects=False)
    assert response.status_code == 303
    with session_scope() as session:
        assert session.get(RecordingProfileOverride, "r1").profile_id == profile_id


def test_status_page_renders(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/status")
    assert r.status_code == 200
    assert "Environment" in r.text and "Pipeline" in r.text and "Configuration" in r.text
    assert "Needs attention" in r.text and "embedding model unavailable" in r.text


def test_settings_editor_renders_models_and_profile_builder(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.get("/settings")
    assert r.status_code == 200
    assert "Model catalog" in r.text
    assert "Add model" in r.text
    assert "Create execution profile" in r.text
    assert "Local only / no egress" in r.text
    assert "New version" in r.text and "Edit" in r.text and "Delete" in r.text
    assert "Remote workers" in r.text and "Register worker" in r.text
    assert 'href="/templates"' in r.text
    assert "Plaud account" in r.text
    assert "Native S256 PKCE · no Node.js" in r.text
    assert "localplaud auth login" in r.text
    assert 'class="settings-nav" aria-label="Settings sections"' in r.text
    for target in (
        "plaud-account",
        "hardware-profiles",
        "vocabulary",
        "note-templates",
        "connections",
        "remote-workers",
        "webhook-integrations",
        "email-integrations",
    ):
        assert f'href="#{target}"' in r.text
        assert f'id="{target}"' in r.text
    assert ".settings-page{max-width:1180px!important}" in r.text
    assert "grid-template-columns:180px minmax(0,1fr)" in r.text
    assert "@media(max-width:820px)" in r.text
    status = c.get("/api/plaud/auth/status").json()
    assert status == {
        "ok": False,
        "detail": f"no session at {tmp_path / 'plaud-tokens.json'}",
        "provider": "official",
        "login_method": "native-pkce-loopback",
    }


def test_export_markdown_endpoint(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/file/r1/export.md")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "Weekly Sync" in r.text and "## Transcript" in r.text
    assert c.get("/file/missing/export.md").status_code == 404


def test_export_menu_and_format_endpoints(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    _seed(str(audio))
    page = c.get("/file/r1")
    assert "Export recording" in page.text
    assert "Speaker labels" in page.text and "Original audio" in page.text
    assert 'data-fmt="docx"' in page.text and 'data-fmt="pdf"' in page.text
    txt = c.get("/file/r1/export/transcript.txt?timestamps=false&speakers=false")
    assert txt.status_code == 200 and "hello team" in txt.text
    assert "SPEAKER_00" not in txt.text and "[00:01]" not in txt.text
    assert c.get("/file/r1/export/transcript.srt").status_code == 200
    vtt = c.get("/file/r1/export/transcript.vtt")
    assert vtt.status_code == 200 and vtt.text.startswith("WEBVTT")
    docx = c.get("/file/r1/export/transcript.docx")
    assert docx.status_code == 200 and docx.content.startswith(b"PK")
    assert "wordprocessingml.document" in docx.headers["content-type"]
    pdf = c.get("/file/r1/export/transcript.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    assert pdf.headers["content-type"].startswith("application/pdf")
    notes_docx = c.get("/file/r1/export/notes.docx")
    assert notes_docx.status_code == 200 and notes_docx.content.startswith(b"PK")
    notes_pdf = c.get("/file/r1/export/notes.pdf")
    assert notes_pdf.status_code == 200 and notes_pdf.content.startswith(b"%PDF-")
    assert c.get("/file/r1/export/transcript.json").status_code == 404
    assert c.get("/file/r1/export/notes.txt").status_code == 200
    assert c.get("/file/r1/export/audio").content == b"audio"


def test_reprocess_missing_audio(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()  # r1 has no audio_path
    assert c.post("/file/r1/reprocess").status_code == 400


def test_search_page_renders_empty(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.get("/search")
    assert r.status_code == 200
    # a query with no index / provider shouldn't 500
    assert c.get("/search?q=anything").status_code == 200


def test_independent_ui_labels_imported_transcript_without_treating_it_as_local(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, Summary, Transcript
    from localplaud.db.session import session_scope

    with session_scope() as s:
        file = PlaudFile(id="cloud", filename="Cloud import", status=FileStatus.downloaded)
        file.transcripts = [
            Transcript(
                provider="plaud",
                source="cloud",
                text="imported text",
                segments=[{"text": "imported text", "start": 0.0, "end": 1.0}],
            )
        ]
        file.summaries = [Summary(template="plaud", source="cloud", content_md="note")]
        s.add(file)

    listing = c.get("/api/files").json()["files"][0]
    assert listing["has_transcript"] is False
    assert listing["has_imported_transcript"] is True
    assert listing["has_summary"] is False
    assert listing["has_imported_summary"] is True

    detail = c.get("/file/cloud")
    assert detail.status_code == 200
    assert "Plaud import" in detail.text
    assert "canonical result" in detail.text
    assert "imported text" in detail.text
