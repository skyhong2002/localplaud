"""Tests for the web UI pages render (dashboard, search, status, detail)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import zipfile
from io import BytesIO

import pytest


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
    assert "All files" in r.text
    assert "rectable" in r.text
    assert "Total audio" not in r.text  # one dense library surface, not a dashboard


def test_library_bulk_export_modal_has_accessible_dialog_contract(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    page = c.get("/")

    assert 'id="bulk-export-open"' in page.text
    assert 'role="dialog" aria-modal="true" aria-labelledby="bulk-export-title"' in page.text
    assert 'id="bulk-export-transcript" type="checkbox" checked' in page.text
    assert 'id="bulk-export-notes" type="checkbox" checked' in page.text
    assert "setExportBackgroundInert(true)" in page.text
    assert "if (event.key === 'Escape')" in page.text
    assert "if (event.shiftKey && document.activeElement === first)" in page.text
    assert "if (restoreFocus) exportOpener?.focus()" in page.text
    assert "fetch('/api/files/export'" in page.text
    assert "signal:cleanupController.signal" in page.text
    assert "if (error.name === 'AbortError') return" in page.text
    assert "response.status === 409 ? tr('No selected recording has an available transcript or note')" in page.text


def test_real_browser_navigation_gets_progressive_library_and_recording_shells(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    _seed()
    headers = {"Sec-Fetch-Dest": "document"}

    library = c.get("/", headers=headers)
    assert library.status_code == 200
    assert 'class="library-page progressive-shell"' in library.text
    assert 'data-progressive-loader' in library.text
    assert 'id="recording-file-list" hx-preserve' not in library.text
    assert "Weekly Sync" not in library.text
    library_workspace = c.get("/?workspace=true", headers=headers)
    assert "Weekly Sync" in library_workspace.text
    assert 'id="recording-file-list" hx-preserve' not in library_workspace.text

    detail = c.get("/file/r1", headers=headers)
    assert detail.status_code == 200
    assert "Weekly Sync" in detail.text
    assert 'class="skeleton-player"' in detail.text
    assert 'id="recording-file-list" hx-preserve' not in detail.text
    assert "SPEAKER_00" not in detail.text
    workspace = c.get("/file/r1?workspace=true", headers=headers)
    assert "SPEAKER_00" in workspace.text
    assert 'id="recording-file-list" hx-preserve' not in workspace.text
    assert 'hx-get="/file/r1/acceptance-panel"' in workspace.text

    htmx_navigation = c.get(
        "/file/r1",
        headers={
            "HX-Request": "true",
            "HX-Target": "app-view",
            "X-Localplaud-Preserve-Filelist": "true",
        },
    )
    assert 'class="skeleton-player"' in htmx_navigation.text
    assert "SPEAKER_00" not in htmx_navigation.text
    assert 'id="recording-file-list" hx-preserve' in htmx_navigation.text
    assert "skeleton-recording" not in htmx_navigation.text
    assert "workspace=true" in htmx_navigation.text
    assert "preserve_filelist=true" in htmx_navigation.text
    assert "<!doctype html>" not in htmx_navigation.text
    assert '<aside class="sidebar">' not in htmx_navigation.text
    assert htmx_navigation.text.count('<div id="app-view" hx-history-elt>') == 1
    assert "<title>Weekly Sync — localplaud</title>" in htmx_navigation.text

    htmx_workspace = c.get(
        "/file/r1?workspace=true&preserve_filelist=true",
        headers={"HX-Request": "true", "HX-Target": "app-view"},
    )
    assert 'class="skeleton-player"' not in htmx_workspace.text
    assert "SPEAKER_00" in htmx_workspace.text
    assert 'id="recording-file-list" hx-preserve' in htmx_workspace.text
    assert "const cleanupController=new AbortController()" in htmx_workspace.text

    history_restore = c.get(
        "/file/r1",
        headers={"HX-Request": "true", "HX-History-Restore-Request": "true"},
    )
    assert "<!doctype html>" not in history_restore.text
    assert '<div id="app-view" hx-history-elt>' in history_restore.text


def test_long_transcript_is_loaded_in_bounded_pages(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        recording = session.get(PlaudFile, "r1")
        recording.local_transcript.segments = [
            {"text": f"segment-{index}", "start": float(index), "end": float(index + 1)}
            for index in range(250)
        ]

    workspace = c.get("/file/r1")
    assert "segment-249" not in workspace.text
    first = c.get("/file/r1/transcript-page?view=raw")
    pinned_id = int(re.search(r"page_transcript_id=(\d+)", first.text).group(1))
    pinned_token = re.search(r"page_transcript_token=([0-9a-f]+)", first.text).group(1)
    assert first.text.count('<div class="seg ') == 120
    assert "offset=120" in first.text
    assert "limit=120" in first.text
    assert "load-all-transcript" not in first.text
    second = c.get(
        f"/file/r1/transcript-page?view=raw&page_transcript_id={pinned_id}"
        f"&page_transcript_token={pinned_token}&offset=120"
    )
    assert second.text.count('<div class="seg ') == 120
    assert "offset=240" in second.text
    final = c.get(
        f"/file/r1/transcript-page?view=raw&page_transcript_id={pinned_id}"
        f"&page_transcript_token={pinned_token}&offset=240"
    )
    assert final.text.count('<div class="seg ') == 10
    assert "segment-249" in final.text
    assert "transcript-page-loader" not in final.text

    from sqlalchemy import delete

    from localplaud.db.models import Transcript
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.execute(delete(Transcript).where(Transcript.id == pinned_id))
        session.add(
            Transcript(
                file_id="r1",
                provider="replacement",
                language="en",
                has_speakers=False,
                source="local",
                text="replacement",
                segments=[{"text": "replacement", "start": 0, "end": 1}],
            )
        )
    stale = c.get(
        f"/file/r1/transcript-page?view=raw&page_transcript_id={pinned_id}"
        f"&page_transcript_token={pinned_token}&offset=120"
    )
    assert stale.status_code == 404


def test_corrected_transcript_pagination_pins_the_revision(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import PlaudFile, TranscriptRevision
    from localplaud.db.session import session_scope

    segments = [
        {"text": f"corrected-{index}", "start": float(index), "end": float(index + 1)}
        for index in range(130)
    ]
    with session_scope() as session:
        recording = session.get(PlaudFile, "r1")
        session.add(
            TranscriptRevision(
                file_id="r1",
                base_transcript_id=recording.local_transcript.id,
                revision=2,
                source="local",
                segments=segments,
                text=" ".join(segment["text"] for segment in segments),
            )
        )
    first = c.get("/file/r1/transcript-page?view=corrected")
    assert "page_revision=2" in first.text
    pinned = c.get("/file/r1/transcript-page?view=corrected&page_revision=2&offset=120")
    assert pinned.status_code == 200
    unpinned = c.get("/file/r1/transcript-page?view=corrected&offset=120")
    assert unpinned.status_code == 409
    missing = c.get("/file/r1/transcript-page?view=corrected&page_revision=999&offset=120")
    assert missing.status_code == 404


def test_transcript_pagination_rejects_empty_and_unknown_continuations(
    monkeypatch, tmp_path
):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="empty", filename="Empty", status=FileStatus.downloaded))

    assert c.get("/file/empty/transcript-page?offset=120").status_code == 409
    assert c.get("/file/empty/transcript-page?source=unexpected").status_code == 422


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

    menu_icon = c.get("/static/lucide/menu.svg")
    assert menu_icon.status_code == 200
    assert menu_icon.headers["content-type"].startswith("image/svg+xml")
    assert b"lucide-menu" in menu_icon.content


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


def test_transcript_page_wraps_speaker_name_for_one_line_clamp(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import Speaker
    from localplaud.db.session import session_scope

    long_name = "陳品妤（產品營運與跨部門協調負責人 Product Operations Lead, Taipei HQ）"
    with session_scope() as s:
        s.add(Speaker(file_id="r1", key="SPEAKER_00", display_name=long_name))

    page = c.get("/file/r1/transcript-page?view=raw")
    assert page.status_code == 200
    # The name is wrapped in .who-name (CSS clamps it to one ellipsized line)
    # and duplicated into title= so the full identity stays reachable.
    assert f'<span class="who" title="{long_name}">' in page.text
    assert f'<span class="who-name">{long_name}</span>' in page.text


def test_ready_to_generate_empty_state_offers_method_dialog(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r2", filename="Fresh recording", status=FileStatus.downloaded,
                        duration_ms=120000, start_time_ms=1783582737000, scene=1,
                        audio_path=str(audio)))

    r = c.get("/file/r2")
    assert r.status_code == 200
    # Guided empty state leads the reading flow instead of a bare status line.
    assert "Ready to generate" in r.text
    assert "The transcript, notes, and mind map will appear here after generation." in r.text
    assert "data-open-generate" in r.text
    assert "Transcript is not available yet." not in r.text
    # Select-generation-method dialog: accessible, method radios, custom rows.
    assert 'id="generate-backdrop"' in r.text
    assert 'role="dialog" aria-modal="true" aria-labelledby="generate-title"' in r.text
    assert "Select generation method" in r.text
    assert "Auto generation" in r.text and "Custom generation" in r.text
    assert 'name="generate-method" value="auto" checked' in r.text
    assert 'id="generate-template"' in r.text
    assert 'id="generate-start"' in r.text and "Start generation" in r.text
    assert 'id="generate-cancel"' in r.text and 'id="generate-close"' in r.text
    assert "The original audio and your edits are never replaced." in r.text
    # The guided empty state stays free of technical pipeline vocabulary.
    empty_block = r.text.split('class="empty generate-empty"', 1)[1].split("</div>", 1)[0]
    for term in ("diarize", "align", "embed", "ASR", "pipeline"):
        assert term not in empty_block
    # Manual notes stay available without a transcript; generated notes remain disabled.
    assert "Create a note now. Generated notes become available after a local transcript exists." in r.text
    assert 'data-open-manual-note' in r.text
    assert 'id="generate-notes" disabled' in r.text


def test_metadata_only_recording_guides_audio_import(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r3", filename="Cloud only", status=FileStatus.metadata_only,
                        duration_ms=90000, start_time_ms=1783582737000, scene=1,
                        audio_path=None))

    r = c.get("/file/r3")
    assert r.status_code == 200
    assert "Audio not imported yet" in r.text
    # Primary reading hierarchy stays free of technical provider language.
    assert "Import the original audio from Plaud first — everything else is generated here afterwards." in r.text
    assert "configured providers" not in r.text.split("Audio not imported yet", 1)[1].split("</div>", 2)[0]
    assert "data-import-audio" in r.text
    # Without local audio there is nothing to generate from yet.
    assert 'id="generate-backdrop"' not in r.text


def test_notes_empty_state_guides_template_generation(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    from localplaud.db.models import FileStatus, PlaudFile, Transcript
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r4", filename="Transcribed only", status=FileStatus.partial,
                        duration_ms=90000, start_time_ms=1783582737000, scene=1,
                        audio_path=str(audio)))
        s.add(Transcript(file_id="r4", provider="mlx-whisper", language="zh", text="你好",
                         segments=[{"text": "你好", "start": 0.0, "end": 1.0}]))

    r = c.get("/file/r4?tab=notes")
    assert r.status_code == 200
    assert "No notes yet." in r.text
    assert "Create your own note or choose a template to generate one from the transcript." in r.text
    assert 'data-open-manual-note' in r.text
    # A local transcript already exists, so the pre-generation dialog is gone.
    assert 'id="generate-backdrop"' not in r.text


def test_note_tabs_scan_outputs_and_mark_editable_copies(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    _seed(str(audio))
    from localplaud.db.models import Summary, UserNote
    from localplaud.db.session import session_scope

    long_title = "跨部門季度回顧與客戶成功追蹤 — extended saved answer about renewals and onboarding blockers"
    with session_scope() as s:
        # A second template output coexists with the seeded meeting note; the
        # data model keeps exactly one current generated output per template.
        s.add(Summary(file_id="r1", template="insights", title="Insights",
                      content_md="# Insights", template_version=3))
        s.add(UserNote(file_id="r1", title=long_title, content_md="body",
                       source_type="ask_answer"))

    r = c.get("/file/r1?tab=notes")
    assert r.status_code == 200
    # One tab per generated template output, keyed by summary id (not index),
    # with version and generation time preserved in the tab title.
    assert r.text.count('class="note-tab ') >= 3  # two generated + one saved
    assert "Insights · v3" in r.text
    import re as _re

    panel_keys = _re.findall(r'data-note-panel="(sum-\d+)"', r.text)
    assert len(panel_keys) == 2 and len(set(panel_keys)) == 2
    for key in panel_keys:
        assert f'data-note-target="{key}"' in r.text
    hidden_panels = _re.findall(r'data-note-panel="sum-\d+" hidden', r.text)
    assert len(hidden_panels) == 1  # only the active output is shown
    # Editable copies are visually distinct (pencil icon, Editable note title)
    # and long titles stay inside the clamped tab with full text on the title.
    assert 'class="note-tab saved-note-tab' in r.text
    assert f'title="Editable note · {long_title}"' in r.text
    saved_tab = r.text.split('class="note-tab saved-note-tab', 1)[1].split("</button>", 1)[0]
    assert "pencil.svg" in saved_tab
    # The "+" affordance creates a user-owned note without invoking generation.
    assert "data-open-manual-note" in r.text
    assert 'aria-label="New note"' in r.text
    assert 'id="manual-note-backdrop"' in r.text
    # Generated-note provenance now carries version and creation time.
    assert _re.search(r"v3 · [A-Z][a-z]{2} \d{2}, \d{4} · \d{2}:\d{2} · Generated from", r.text)
    # Lockstep invariant: the highlighted tab's target is exactly the one
    # panel rendered visible, independent of either list's ordering.
    active_target = _re.search(
        r'data-note-target="((?:sum|saved)-\d+)" class="note-tab on"', r.text
    ).group(1)
    assert f'data-note-panel="{active_target}" hidden' not in r.text
    assert f'data-note-panel="{active_target}"' in r.text
    for other in set(panel_keys) - {active_target}:
        assert f'data-note-panel="{other}" hidden' in r.text


def test_saved_only_recording_activates_saved_tab_and_panel(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r6", filename="Saved only", status=FileStatus.metadata_only,
                        duration_ms=30000, start_time_ms=1783582737000, scene=1,
                        audio_path=None))
        s.add(UserNote(file_id="r6", title="Kept answer", content_md="body",
                       source_type="ask_answer"))

    r = c.get("/file/r6?tab=notes")
    assert r.status_code == 200
    import re as _re

    saved_id = _re.search(r'data-note-target="(saved-\d+)"', r.text).group(1)
    # With no generated notes, the saved tab is active and its panel visible.
    assert f'data-note-target="{saved_id}" class="note-tab saved-note-tab on"' in r.text
    assert f'data-note-panel="{saved_id}" hidden' not in r.text
    assert f'data-note-panel="{saved_id}"' in r.text


def test_generate_dialog_auto_persists_auto_template_choice(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r5", filename="Untranscribed", status=FileStatus.downloaded,
                        duration_ms=60000, start_time_ms=1783582737000, scene=1,
                        audio_path=str(audio)))

    r = c.get("/file/r5")
    # Auto must persist note-template key "auto" before queueing, so a prior
    # per-recording custom template cannot be silently reused under the Auto
    # label; Custom persists the picked key through the same call.
    assert "const key=custom?document.getElementById('generate-template').value:'auto';" in r.text
    assert r.text.index("const key=custom?") < r.text.index("/reprocess',{method:'POST'}")


def test_processing_recording_shows_friendly_progress(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r7", filename="Being generated", status=FileStatus.processing,
                        duration_ms=60000, start_time_ms=1783582737000, scene=1,
                        audio_path=str(audio)))

    r = c.get("/file/r7")
    assert r.status_code == 200
    # Friendly in-progress state without stage jargon, updating politely.
    assert '<div class="empty generate-empty" data-generation-progress>' in r.text
    assert 'id="generation-progress-title">Generating…' in r.text
    assert 'aria-live="polite" id="generation-progress-note"' in r.text
    assert "This page updates automatically when results are ready." in r.text
    progress_block = r.text.split(
        '<div class="empty generate-empty" data-generation-progress>', 1
    )[1].split("</div>", 1)[0]
    for term in ("diarize", "align", "embed", "ASR", "pipeline", "stage"):
        assert term not in progress_block
    # No Generate CTA while work is pending, and the header label is live.
    assert 'data-open-generate><span' not in r.text
    assert 'id="recording-state-label" aria-live="polite">Generating…' in r.text
    # The poller reuses the existing read-only status endpoint with cleanup.
    assert "/api/imports/plaud/files/r7/audio/status" in r.text
    assert "if(statusTimer)clearTimeout(statusTimer)" in r.text
    # Hardened terminal detection: only a 2xx body with a recognized string
    # status may refresh the view; anything else retries calmly, never reloads.
    assert "if(response.ok)data=await response.json();" in r.text
    assert "if(cleanupController.signal.aborted)return;" in r.text
    assert "const status=typeof data?.status==='string'?data.status:null;" in r.text
    assert "const terminalStates=['done','partial','error'];" in r.text
    assert "statusTimer=setTimeout(tick,7000);" in r.text
    # Terminal refresh is a one-time hard reload issued only after the abort
    # re-check: it cannot race an HTMX navigation that detached #app-view the
    # way an in-flight htmx.ajax swap could, and the URL keeps tab context.
    assert "htmx.ajax" not in r.text.split("terminalStates.includes(status)", 1)[1].split("}", 3)[0]
    assert r.text.index("if(cleanupController.signal.aborted)return;") < r.text.index("terminalStates.includes(status)")
    assert "location.reload();" in r.text.split("terminalStates.includes(status)", 1)[1][:400]
    assert ".generate-progress-icon { animation:generate-spin" in r.text
    assert "@media (prefers-reduced-motion: no-preference)" in r.text


def test_queued_flag_shows_queued_state_only_when_pending(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r8", filename="Just queued", status=FileStatus.downloaded,
                        duration_ms=60000, start_time_ms=1783582737000, scene=1,
                        audio_path=str(audio)))

    queued = c.get("/file/r8?queued=1")
    assert "Queued for generation" in queued.text
    assert '<div class="empty generate-empty" data-generation-progress>' in queued.text
    plain = c.get("/file/r8")
    assert '<div class="empty generate-empty" data-generation-progress>' not in plain.text
    assert "Ready to generate" in plain.text
    assert 'data-open-generate><span' in plain.text


def test_error_recording_keeps_usable_content_visible(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    from localplaud.db.models import FileStatus, PlaudFile, Transcript
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r9", filename="Failed later stage", status=FileStatus.error,
                        duration_ms=60000, start_time_ms=1783582737000, scene=1,
                        audio_path=str(audio), error="embedding model unavailable"))
        s.add(Transcript(file_id="r9", provider="mlx-whisper", language="zh", text="内容",
                         segments=[{"text": "内容", "start": 0.0, "end": 1.0}]))

    r = c.get("/file/r9")
    # The transcript stays first-class; the failure shows as the existing
    # actionable alert, never as a progress state that hides content.
    assert '<div class="empty generate-empty" data-generation-progress>' not in r.text
    assert "Processing paused" in r.text
    assert 'id="transcript"' in r.text and "/file/r9/transcript-page" in r.text


def test_sidebar_ops_card_summarizes_workspace(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    _seed(str(audio))  # r1 done
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="ra", filename="Working", status=FileStatus.processing,
                        duration_ms=1000, start_time_ms=0, audio_path=str(audio)))
        s.add(PlaudFile(id="rb", filename="Broken", status=FileStatus.error,
                        duration_ms=1000, start_time_ms=0, audio_path=str(audio)))

    r = c.get("/")
    assert "Workspace status" in r.text
    card = r.text.split('data-testid="ops-card"', 1)[1].split("</div>", 1)[0]
    # Each count is an individually reachable link into the Library filters,
    # plus a single System status destination.
    assert '<a class="ops-stat" href="/?state=generating"><strong>1</strong> generating</a>' in card
    assert '<a class="ops-stat" href="/?state=attention"><strong class="ops-attn">1</strong> need attention</a>' in card
    assert '<a class="ops-stat" href="/?state=done"><strong>1</strong> ready</a>' in card
    assert '<a class="ops-sub" href="/status">View system status</a>' in card
    assert ".ops-stat:focus-visible,.ops-card .ops-sub:focus-visible { outline:2px solid var(--blue)" in r.text


def test_sidebar_ops_card_all_caught_up(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    _seed(str(audio))  # only r1, status done

    r = c.get("/")
    card = r.text.split('data-testid="ops-card"', 1)[1].split("</div>", 1)[0]
    assert "All caught up" in card
    assert "generating" not in card and "need attention" not in card and "in cloud" not in card


def test_detail_page_renders(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    _seed(str(audio))
    r = c.get("/file/r1")
    assert r.status_code == 200
    assert "SPEAKER_00" in r.text
    assert 'id="app-view" hx-history-elt' in r.text
    assert '<aside class="sidebar" id="workspace-sidebar">' in r.text
    assert '<nav class="product-rail"' in r.text
    assert 'data-sidebar-toggle aria-label="Toggle sidebar" aria-expanded="true"' in r.text
    assert "localplaud:sidebar-collapsed" in r.text
    assert ".mm-label" in r.text and "overflow-wrap:anywhere" in r.text
    assert "body.nav-open { overflow:hidden; }" in r.text
    assert ".title-edit { width:36px;height:36px;opacity:1; }" in r.text
    assert ".seg .editbtn { min-width:36px;min-height:36px;opacity:1; }" in r.text
    assert "max-height:calc(100dvh - 24px);overflow:hidden" in r.text
    assert ".import-body { min-height:0;" in r.text and "overflow-y:auto" in r.text
    assert ".ask-user-message" in r.text and ".saved-note-actions" in r.text
    assert "@media (min-width:1121px){ body.sidebar-collapsed .sidebar { display:none; } }" in r.text
    assert r.text.index('href="/" title="All files"') < r.text.index('href="/search" title="Search"')
    assert r.text.index('href="/search" title="Search"') < r.text.index(
        'href="/?ask=true#library-ask" title="Ask localplaud"'
    )
    assert r.text.index('href="/?ask=true#library-ask" title="Ask localplaud"') < r.text.index(
        'href="/templates" title="Templates"'
    )
    assert 'aria-controls="workspace-sidebar" aria-expanded="false"' in r.text
    assert 'class="nav-scrim" type="button" data-nav-close aria-label="Close menu" hidden' in r.text
    assert "event.target.closest('a.navi[href]')" in r.text
    assert "document.activeElement===last" in r.text
    assert 'data-open-import="device" aria-label="Add audio"' in r.text
    assert "event.target.closest('[data-open-import]')" in r.text
    assert "getElementById('add-audio-button')?.click()" not in r.text
    assert 'id="recording-file-list" hx-preserve' in r.text
    assert 'class="backlink" href="/" hx-get="/" hx-target="#app-view"' in r.text
    assert "link.setAttribute('hx-target','#app-view')" in r.text
    assert "(?:api|audio|static|login|logout|oauth)" in r.text
    assert "link.setAttribute('hx-boost','false')" in r.text
    assert "sessionStorage.setItem(storageKey()" in r.text
    assert "if(event.target===appView)cleanupController.abort()" in r.text
    assert "const drainTranscript=()=>" in r.text
    assert ".tabs::-webkit-scrollbar { display:none; }" in r.text
    assert ".sidebar-scroll" in r.text
    assert 'data-start' in r.text  # seekable segments
    assert "meeting" in r.text.lower()  # summary tab
    assert "Processing details" in r.text
    assert ".recording-advanced:not([open]) { display:none; }" in r.text
    assert "event.currentTarget.closest('details')?.removeAttribute('open')" in r.text
    assert "details.open=true;details.scrollIntoView" in r.text
    assert "#reprocess-msg:empty { display:none; }" in r.text
    assert 'id="reprocess-msg" class="sub" aria-live="polite"' in r.text
    assert 'class="pane recording-pane has-player"' in r.text
    assert ".recording-pane.has-player .tabs { top:92px; }" in r.text
    assert ".tabs { position:sticky;top:8px;" in r.text
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
    assert 'id="open-share" type="button" aria-label="Share" title="Share"' in r.text
    assert 'id="open-export" type="button" aria-label="Export" title="Export"' in r.text
    assert 'class="tabs" role="tablist"' in r.text
    assert 'id="recording-tab-transcript" role="tab"' in r.text
    assert 'id="recording-panel-transcript" role="tabpanel"' in r.text
    assert "item.setAttribute('aria-selected',String(active))" in r.text
    assert "item.tabIndex=active?0:-1" in r.text
    assert "function openDialog(backdrop,opener)" in r.text
    assert "recordingShell.inert=true" in r.text
    assert "dialogChromeRegions.forEach(region=>{region.inert=true;})" in r.text
    assert "event.key==='Escape'" in r.text
    assert "event.key!=='Tab'" in r.text
    assert "dialogOpener?.focus()" in r.text
    assert "body.dialog-open { overflow:hidden; }" in r.text
    assert 'data-close-popover aria-label="Close"' in r.text
    assert "const workspacePopovers=" in r.text
    assert "closeWorkspacePopover(open)" in r.text
    assert ".md table { display:block;max-width:100%;overflow-x:auto; }" in r.text
    # Cells must opt out of the .md overflow-wrap:anywhere default: anywhere
    # collapses every column's min-content width to one character on narrow
    # viewports, so wide tables squeeze into vertical letter stacks instead of
    # engaging the horizontal table scroll asserted above.
    assert "vertical-align:top;overflow-wrap:break-word; }" in r.text
    # The clock shows the recording's stored duration before the browser has
    # loaded audio metadata; playback sync falls back to the same value.
    assert ">0:00 / 10:00</span>" in r.text
    assert "const knownDuration=600.0;" in r.text
    assert "d=player.duration||knownDuration" in r.text
    # Speaker labels clamp to one visual line; the full name stays in the
    # legend, hover title, and exports.
    assert (
        ".seg .who .who-name { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }"
        in r.text
    )
    assert ".speaker-pill>summary{max-width:min(200px,58vw)}" in r.text
    assert "availableHeight=Math.max(1,viewport.clientHeight-30)" in r.text
    assert "availableHeight/natural.height*100" in r.text
    assert "zoom:var(--mm-scale)" in r.text
    assert 'id="subscription-independence"' in r.text
    assert "Subscription independence" in r.text
    assert 'data-summary-copy=' in r.text
    assert 'id="open-share"' in r.text and 'id="share-backdrop" hidden' in r.text
    assert 'id="generate-notes"' in r.text
    assert "Choose a template, then generate notes and mind map." not in r.text
    assert 'id="generate-backdrop"' not in r.text  # transcript exists — no pre-generation dialog
    assert '<details class="speaker-pill">' in r.text
    assert '<form class="speaker-editor" method="post" action="/file/r1/speakers">' in r.text
    assert '<h1>Sync</h1>' in r.text
    assert 'id="recording-title-display"' in r.text and 'id="edit-recording-title"' in r.text
    assert 'id="benchmark-backdrop"' not in r.text
    assert 'id="open-benchmark"' not in r.text

    filtered = c.get(
        "/file/r1",
        params={"return_to": "/?view=uncategorized&page=2&folder=7"},
    )
    assert (
        'class="backlink" href="/?view=uncategorized&amp;page=2&amp;folder=7" '
        'hx-get="/?view=uncategorized&amp;page=2&amp;folder=7"'
    ) in filtered.text
    external = c.get("/file/r1", params={"return_to": "https://example.com/"})
    assert 'class="backlink" href="/" hx-get="/"' in external.text
    searched = c.get(
        "/file/r1",
        params={"return_to": "/?q=Weekly&sort=name&dir=asc&page=1", "tab": "notes"},
    )
    assert '<div class="fl-title">Search results</div>' in searched.text
    assert '<input class="fl-search" name="q" value="Weekly"' in searched.text
    assert 'data-panel="notes" class="on"' in searched.text
    assert 'data-panel="notes" >' in searched.text
    notes_workspace = c.get("/file/r1?tab=notes")
    assert '/file/r1?return_to=%2F&amp;tab=notes' in notes_workspace.text
    assert notes_workspace.text.index('class="ask"') < notes_workspace.text.index(
        'id="ask-chips"'
    )
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
        acceptance_panel = c.get("/file/r1/acceptance-panel")
    assert page.status_code == 200
    assert '<html lang="zh-Hant-TW"' in page.text
    assert "const tr=window.localplaudT" in page.text
    assert "output.textContent=tr('Removing local data…')" in page.text
    assert "output.textContent=tr('Replacing…')" in page.text
    assert "if(label)label.textContent=tr('Importing…')" in page.text
    assert "out.textContent=tr('Checking recording signals…')" in page.text
    for text in (
        "儲存標題",
        "繼續處理",
        "全部重建",
        "本機資料",
        "執行設定檔",
        "筆記範本",
        "講者名稱",
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
            "處理已暫停",
            "使用目前的 AI 設定檔繼續，並重新開始重試次數",
            "技術詳情",
    ):
        assert text in page.text
    assert acceptance_panel.status_code == 200
    assert "尚未通過" in acceptance_panel.text
    assert "JSON 證據" in acceptance_panel.text


def test_safe_markdown_renderer_supports_notes_without_html_or_unsafe_links():
    from localplaud.api.app import _render_markdown

    rendered = str(
        _render_markdown(
            "# Heading\n\n"
            "- outer\n  - inner\n\n"
            "| Name | Value |\n| --- | --- |\n| A | ~~B~~ |\n\n"
            "<script>alert('x')</script>\n\n"
            "[unsafe](javascript:alert(1)) [safe](https://example.com/path)\n\n"
            "![tracking pixel](https://tracker.example/pixel.gif)"
        )
    )
    assert "<h1>Heading</h1>" in rendered
    assert rendered.count("<ul>") == 2
    assert "<table>" in rendered and "<s>B</s>" in rendered
    assert "&lt;script&gt;" in rendered and "<script>" not in rendered
    assert 'href="javascript:' not in rendered
    assert 'href="https://example.com/path"' in rendered
    assert "<img" not in rendered and "tracking pixel" in rendered


def test_metadata_only_plaud_recording_offers_audio_import(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/file/r1")
    assert r.status_code == 200
    assert "Import audio" in r.text
    assert 'hx-post="/api/files/r1/reprocess"' not in r.text
    assert 'hx-post="/api/files/r1/reprocess?force=true"' not in r.text
    button = re.search(r'<button class="btn" type="button" id="generate-notes"([^>]*)>', r.text)
    assert button and "disabled" not in button.group(1)


def test_imported_only_recording_disables_local_generation(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__ARTIFACT_MODE", "migration")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__PREFER_CLOUD_ARTIFACTS", "true")
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, Transcript
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            PlaudFile(
                id="cloud-only-ui",
                filename="Imported transcript",
                status=FileStatus.done,
                transcripts=[
                    Transcript(
                        provider="plaud",
                        source="cloud",
                        text="Imported only",
                        segments=[{"text": "Imported only", "start": 0.0, "end": 1.0}],
                    )
                ],
            )
        )

    page = c.get("/file/cloud-only-ui?tab=notes")
    assert 'class="pane recording-pane"' in page.text
    assert 'class="pane recording-pane has-player"' not in page.text
    button = re.search(
        r'<button class="btn" type="button" id="generate-notes"([^>]*)>', page.text
    )
    assert button and "disabled" in button.group(1)
    assert "A local transcript is required first." in page.text
    assert re.search(r'id="ask-q"[^>]*disabled', page.text)
    assert re.search(r'class="btn sec ask-chip"[^>]*disabled', page.text)
    assert c.post("/file/cloud-only-ui/generate-notes").status_code == 409


def test_speaker_rename_preserves_library_context(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()

    class DeferredThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("threading.Thread", DeferredThread)
    response = c.post(
        "/file/r1/speakers",
        data={
            "key": "SPEAKER_00",
            "name": "Alex",
            "return_to": "/?view=uncategorized&page=2",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/file/r1?return_to=%2F%3Fview%3Duncategorized%26page%3D2&tab=transcript"
    )


def test_recording_profile_picker_persists_override(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import (
        ExecutionProfile,
        ModelCatalogEntry,
        ProfileStageSelection,
        ProviderConnection,
        RecordingProfileOverride,
    )
    from localplaud.db.session import session_scope

    with session_scope() as session:
        profile_id = session.query(ExecutionProfile.id).filter_by(is_system_default=True).scalar()
    response = c.post("/file/r1/profile", data={"profile_id": profile_id}, follow_redirects=False)
    assert response.status_code == 303
    with session_scope() as session:
        assert session.get(RecordingProfileOverride, "r1").profile_id == profile_id
    page = c.get("/file/r1")
    assert "Automatic" in page.text and "Resolved layers" in page.text
    resolution = c.get("/api/providers/recordings/r1/resolution")
    assert resolution.status_code == 200
    assert resolution.json()["resolved"]["schema"] == "localplaud-resolved-profile/v2"

    cleared = c.post(
        "/file/r1/profile", data={"profile_id": ""}, follow_redirects=False
    )
    assert cleared.status_code == 303
    with session_scope() as session:
        assert session.get(RecordingProfileOverride, "r1") is None

    with session_scope() as session:
        connection = ProviderConnection(
            key="invalid:test",
            name="Invalid",
            provider_type="test",
            execution_target="local",
            data_egress=False,
        )
        session.add(connection)
        session.flush()
        model = ModelCatalogEntry(
            connection_id=connection.id,
            model_key="invalid",
            display_name="Invalid",
            capabilities={"execution_target": "local", "data_egress": False, "stages": []},
        )
        profile = ExecutionProfile(key="invalid", name="Invalid", version=1)
        session.add_all([model, profile])
        session.flush()
        session.add(
            ProfileStageSelection(
                profile_id=profile.id,
                stage="summarize",
                connection_id=connection.id,
                model_id=model.id,
            )
        )
        session.add(RecordingProfileOverride(file_id="r1", profile_id=profile.id))
    invalid = c.get("/api/providers/recordings/r1/resolution")
    assert invalid.status_code == 422
    degraded = c.get("/file/r1")
    assert degraded.status_code == 200
    assert "Profile resolution needs attention" in degraded.text


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
    assert "<script>\n(()=>{\nconst CONNECTIONS=" in r.text
    assert "Object.assign(window,{editVocabulary" in r.text
    templates = c.get("/templates")
    assert templates.status_code == 200
    assert "<script>\n(()=>{\nconst tr=window.localplaudT" in templates.text
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
    assert 'id="copy-transcript"' in page.text and 'id="copy-notes"' in page.text
    assert "navigator.clipboard.writeText(text)" in page.text
    assert "document.execCommand('copy')" in page.text
    assert "priorFocus?.focus()" in page.text
    assert "export/transcript.txt?timestamps=${timestamps}&speakers=${speakers}" in page.text
    assert "fetch('/file/r1/export/notes.md',{signal:cleanupController.signal})" in page.text
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


def test_bulk_export_route_streams_zip_and_reports_partial_availability(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="bare", filename="No derived content"))

    response = c.post(
        "/api/files/export",
        json={
            "file_ids": ["r1", "bare"],
            "transcript_format": "txt",
            "notes_format": "md",
            "timestamps": False,
            "speakers": False,
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["content-disposition"] == (
        'attachment; filename="localplaud-recordings.zip"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-localplaud-export-emitted"] == "2"
    assert response.headers["x-localplaud-export-skipped"] == "2"
    assert int(response.headers["content-length"]) == len(response.content)
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        transcript_name = next(name for name in archive.namelist() if name.endswith("transcript.txt"))
        transcript = archive.read(transcript_name).decode()
    assert "hello team" in transcript
    assert "SPEAKER_00" not in transcript and "[00:01]" not in transcript
    assert [row["id"] for row in manifest["recordings"]] == ["r1", "bare"]
    assert c.post(
        "/api/files/export", json={"file_ids": ["missing"], "transcript_format": "txt"}
    ).status_code == 404
    assert c.post(
        "/api/files/export", json={"file_ids": ["bare"], "notes_format": "md"}
    ).status_code == 409
    assert c.post("/api/files/export", json={"file_ids": ["r1"]}).status_code == 422


def test_bulk_export_stream_closes_on_client_disconnect(monkeypatch):
    from starlette.requests import ClientDisconnect

    from localplaud.api.app import BulkExportBody, bulk_export_files
    from localplaud.bulk_export import BulkExportResult

    stream = BytesIO(b"archive")
    result = BulkExportResult(
        stream=stream,
        size_bytes=7,
        manifest={
            "recordings": [
                {"outputs": [{"status": "emitted"}]},
            ]
        },
    )
    monkeypatch.setattr("localplaud.bulk_export.build_bulk_export", lambda _request: result)
    response = bulk_export_files(
        BulkExportBody(file_ids=["r1"], transcript_format="txt")
    )

    async def disconnect():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        await response(
            {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}},
            receive,
            send,
        )

    with pytest.raises(ClientDisconnect):
        asyncio.run(disconnect())
    assert stream.closed


def test_saved_note_only_recording_can_open_export_and_copy_notes(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="notes-only", filename="Notes only"))
        session.add(
            UserNote(
                file_id="notes-only",
                title="Durable note",
                content_md="Local content",
                source_type="manual",
            )
        )

    page = c.get("/file/notes-only")
    assert page.status_code == 200
    assert 'id="open-export"' in page.text
    assert 'id="copy-notes"' in page.text
    assert 'id="copy-transcript"' not in page.text
    assert 'href="/file/notes-only/export.md"' in page.text
    exported = c.get("/file/notes-only/export/notes.md")
    assert exported.status_code == 200 and "Local content" in exported.text


def test_reprocess_missing_audio(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()  # r1 has no audio_path
    assert c.post("/file/r1/reprocess").status_code == 400


def test_generate_notes_only_queues_derived_stages(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()  # the derived-only path deliberately works after local audio removal
    started = []

    class DeferredThread:
        def __init__(self, *, target, args=(), kwargs=None, daemon=None):
            self.target = target
            self.args = args

        def start(self):
            started.append((self.target, self.args))

    monkeypatch.setattr("threading.Thread", DeferredThread)
    response = c.post("/file/r1/generate-notes")
    assert response.status_code == 200
    assert response.text == "notes and mind map queued"
    assert started and started[0][1] == ("r1",)
    assert started[0][0].__name__ == "process_derived_artifacts"

    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageStatus
    from localplaud.db.session import session_scope

    with session_scope() as session:
        recording = session.get(PlaudFile, "r1")
        stages = {run.stage: run for run in recording.stage_runs}
        assert recording.status == FileStatus.partial
        for stage in (StageName.summarize, StageName.mind_map, StageName.index):
            assert stages[stage].status == StageStatus.pending
            assert stages[stage].detail["stale"] is True
            assert stages[stage].detail["derived_only"] is True
        assert stages[StageName.correct].status == StageStatus.completed
        assert stages[StageName.align].status == StageStatus.completed


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
                text=" ".join(f"imported-{index}" for index in range(130)),
                segments=[
                    {
                        "text": f"imported-{index}",
                        "start": float(index),
                        "end": float(index + 1),
                    }
                    for index in range(130)
                ],
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
    assert 'hx-get="/file/cloud/transcript-page?source=imported"' in detail.text
    first = c.get("/file/cloud/transcript-page?source=imported")
    assert "imported-0" in first.text and "offset=120" in first.text
    transcript_id = int(re.search(r"page_transcript_id=(\d+)", first.text).group(1))
    transcript_token = re.search(
        r"page_transcript_token=([0-9a-f]+)", first.text
    ).group(1)
    continuation = c.get(
        f"/file/cloud/transcript-page?source=imported&page_transcript_id={transcript_id}"
        f"&page_transcript_token={transcript_token}&offset=120"
    )
    assert continuation.status_code == 200 and "imported-129" in continuation.text

    with session_scope() as session:
        local_row = Transcript(
            file_id="cloud",
            provider="local-asr",
            source="local",
            text="local raw",
            segments=[{"text": "local raw", "start": 0.0, "end": 1.0}],
        )
        other = PlaudFile(id="other", filename="Other", status=FileStatus.downloaded)
        other_imported = Transcript(
            provider="plaud",
            source="cloud",
            text="other imported",
            segments=[{"text": "other imported", "start": 0.0, "end": 1.0}],
        )
        other.transcripts = [other_imported]
        session.add_all([local_row, other])
        session.flush()
        local_row_id = local_row.id
        other_imported_id = other_imported.id

    wrong_file = c.get(
        f"/file/cloud/transcript-page?source=imported&page_transcript_id={other_imported_id}"
        f"&page_transcript_token={transcript_token}&offset=120"
    )
    assert wrong_file.status_code == 404
    wrong_source = c.get(
        f"/file/cloud/transcript-page?source=imported&page_transcript_id={local_row_id}"
        f"&page_transcript_token={transcript_token}&offset=120"
    )
    assert wrong_source.status_code == 404
    assert c.get(
        "/file/cloud/transcript-page?source=imported&offset=120"
    ).status_code == 409
    assert c.get(
        f"/file/cloud/transcript-page?source=imported&page_transcript_id={transcript_id}"
        "&page_transcript_token=deadbeef&offset=120"
    ).status_code == 404

    with session_scope() as session:
        imported = session.get(Transcript, transcript_id)
        imported.segments = [
            {"text": "refreshed imported", "start": 0.0, "end": 1.0}
        ]
    mutated = c.get(
        f"/file/cloud/transcript-page?source=imported&page_transcript_id={transcript_id}"
        f"&page_transcript_token={transcript_token}&offset=120"
    )
    assert mutated.status_code == 404


def test_home_modules_use_shell_language_and_direct_routes(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()  # r1 done
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    long_title = "二○二六 Q3 跨部門客戶訪談與後續追蹤決議（含負責人與驗收條件）extended weekly review"
    with session_scope() as s:
        s.add(PlaudFile(id="h-gen", filename=long_title, status=FileStatus.processing,
                        duration_ms=90000, start_time_ms=1783582838000, scene=1))
        s.add(PlaudFile(id="h-err", filename="Failed import", status=FileStatus.error,
                        duration_ms=30000, start_time_ms=1783582839000, scene=1))

    r = c.get("/home")
    assert r.status_code == 200
    # Ops vocabulary and the aggregate filter route replace the old tile.
    assert ">Generating</div>" in r.text
    assert 'href="/?state=generating"' in r.text
    assert "Processing now" not in r.text and "Durable local stages" not in r.text
    # Title-first rows with one muted meta line and direct recording routes.
    assert 'class="home-row" href="/file/h-gen"' in r.text
    assert f'<strong class="home-row-title">{long_title}</strong>' in r.text
    assert ".home-row-title { font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis; }" in r.text
    # Friendly status chips: quiet done rows, Generating… while working.
    row_gen = r.text.split('href="/file/h-gen"', 1)[1].split("</a>", 1)[0]
    assert "Generating…" in row_gen
    row_done = r.text.split('href="/file/r1"', 1)[1].split("</a>", 1)[0]
    assert '<span class="st' not in row_done
    # Needs attention: translated heading, aggregate route, friendly chip.
    assert "Needs attention" in r.text
    assert 'href="/?state=attention"' in r.text and "View all →" in r.text
    row_err = r.text.split('href="/file/h-err"', 1)[1].split("</a>", 1)[0]
    assert '<span class="st error">' in row_err


def test_search_results_are_title_first_with_quiet_kind_labels(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import Folder, PlaudFile, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as s:
        folder = Folder(name="跨部門產品營運與客戶成功長期追蹤資料夾")
        s.add(folder)
        s.flush()
        s.get(PlaudFile, "r1").folder_id = folder.id
        s.add(UserNote(file_id="r1", title="Kept answer",
                       content_md="hello darkness my old friend", source_type="ask_answer"))

    r = c.get("/search?q=hello")
    assert r.status_code == 200
    # Title-first group header with duration · date · folder context, owned
    # search styling with a stable two-line clamp, full text on title=.
    assert '<strong class="search-group-title">Weekly Sync</strong>' in r.text
    assert 'title="Weekly Sync"' in r.text
    assert "-webkit-line-clamp:2" in r.text
    assert "@media (max-width:700px){ .search-hit-text { display:-webkit-box;-webkit-line-clamp:3" in r.text
    head = r.text.split('class="search-group-head"', 1)[1].split("</a>", 1)[0]
    assert "10:00 ·" in head and "跨部門產品營運與客戶成功長期追蹤資料夾" in head
    # Transcript match: the playable timestamp is the label — no jargon chips.
    assert 'href="/file/r1?t=1.0"' in r.text
    assert ">Semantic<" not in r.text and ">Transcript<" not in r.text
    # The saved note is distinguished from generated notes.
    assert '<span class="search-kind">Saved note</span>' in r.text
    assert "recordings matched" in r.text

    # Generated-note match keeps the quiet Note label.
    notes = c.get("/search?q=point")
    assert '<span class="search-kind">Note</span>' in notes.text

    # A title match renders compactly instead of duplicating the heading.
    titles = c.get("/search?q=Weekly")
    assert "Matches the recording title" in titles.text
    title_hit = titles.text.split("Matches the recording title", 1)[0].rsplit('class="search-hit"', 1)[1]
    assert "Weekly Sync" not in title_hit
    # No-query and no-match states offer direct next actions, no marketing.
    empty = c.get("/search")
    assert "Search your whole library" in empty.text
    for href, label in (("/", "All files"), ("/notes", "Saved notes"),
                        ("/?ask=true#library-ask", "Ask the library")):
        assert f'href="{href}"' in empty.text and label in empty.text
    missing = c.get("/search?q=zzznotfoundzzz")
    assert "No matches for" in missing.text and "Ask the library" in missing.text
