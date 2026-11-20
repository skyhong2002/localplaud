"""Row "Generate notes" action (phone swipe + row "…" menu).

The action only opens the recording's existing local Generate notes form.
It must be offered only for recordings with a canonical local transcript that
are not in Trash and not already generating, and it must never queue work by
itself: the user still confirms in the form.
"""

from __future__ import annotations

import re
from html import unescape

from tests.assets import with_assets


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'gen.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed():
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
    )
    from localplaud.db.session import session_scope

    def local(text="hello"):
        return [Transcript(provider="faster-whisper", language="en", source="local", text=text,
                           segments=[{"text": text, "start": 0.0, "end": 1.0}])]

    with session_scope() as s:
        s.add(PlaudFile(id="ready", filename="Ready local", status=FileStatus.done,
                        start_time_ms=5000, transcripts=local()))
        s.add(PlaudFile(id="trashed", filename="Trashed local", status=FileStatus.done,
                        start_time_ms=4000, is_trash=True, transcripts=local()))
        s.add(PlaudFile(id="cloudonly", filename="Cloud only", status=FileStatus.done,
                        start_time_ms=3000,
                        transcripts=[Transcript(provider="plaud", source="cloud", text="x",
                                                segments=[{"text": "x", "start": 0.0, "end": 1.0}])]))
        s.add(PlaudFile(id="busy", filename="Busy notes", status=FileStatus.processing,
                        start_time_ms=2000, transcripts=local(),
                        stage_runs=[StageRun(stage=StageName.summarize, status=StageStatus.running)]))
        s.add(PlaudFile(id="noaudio", filename="No transcript", status=FileStatus.downloaded,
                        start_time_ms=1000))


def _row_flags(html: str) -> dict[str, str]:
    flags = {}
    for tag in re.findall(r'<button class="icon-btn row-more"[^>]*>', html):
        file_id = re.search(r'data-file-id="([^"]+)"', tag).group(1)
        flags[file_id] = re.search(r'data-can-generate="([01])"', tag).group(1)
    return flags


def _eligible(html: str) -> str:
    return re.search(r'id="note-generate-backdrop"[^>]*data-note-generate-eligible="([01])"',
                     html).group(1)


def test_library_rows_mark_only_eligible_recordings(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    flags = _row_flags(c.get("/").text)
    assert flags == {"ready": "1", "cloudonly": "0", "busy": "0", "noaudio": "0"}
    trash = _row_flags(c.get("/?view=trash").text)
    assert trash == {"trashed": "0"}


def test_api_summary_exposes_canonical_local_transcript(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    files = {f["id"]: f for f in c.get("/api/files").json()["files"]}
    assert files["ready"]["has_local_transcript"] is True
    assert files["cloudonly"]["has_local_transcript"] is False


def test_row_menu_has_keyboard_generate_item_and_swipe_wiring(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    page = c.get("/")
    menu = re.search(r'<div class="menu row-menu" id="row-menu".*?</div>\s*<dialog', page.text, re.S)
    item = re.search(r'<a class="menu-item" role="menuitem" href="#" data-row-action="generate" hidden>',
                     menu.group(0))
    assert item, "Generate notes must be a focusable menuitem in the shared row menu"
    assert "Generate notes" in unescape(menu.group(0))
    shell = with_assets(c, page)
    # Menu item and swipe action share one URL builder that only opens the form.
    assert "lp.generateNotesHref = fileId => `/file/${encodeURIComponent(fileId)}?tab=notes&generate=notes`" in shell
    assert "generate.hidden = row.canGenerate !== '1' || row.trash === '1';" in shell
    assert "more.dataset.canGenerate === '1' ? [['generate', 'sparkles', 'Generate notes']] : []" in shell
    assert "if (action === 'generate' && (data.canGenerate !== '1' || data.trash === '1')) return;" in shell
    # The shared menu reuses one <a> per item, so plain activations navigate
    # through a fresh link that progressive navigation can convert.
    assert "#row-menu a[data-row-action=\"generate\"]" in shell
    assert "link.addEventListener('htmx:afterRequest', cleanup, {once: true});" in shell
    # No generation request is issued from the list.
    assert "generate-notes" not in shell.split("lp.rowAction = (button, action) =>", 1)[1].split("menu.addEventListener", 1)[0]


def test_detail_generate_param_opens_form_only_when_eligible(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from sqlalchemy import func, select

    from localplaud.db.models import StageRun
    from localplaud.db.session import session_scope

    with session_scope() as s:
        before = s.scalar(select(func.count()).select_from(StageRun))

    ready = c.get("/file/ready?tab=notes&generate=notes")
    assert ready.status_code == 200
    assert _eligible(ready.text) == "1"
    # The handoff strips the one-shot param and only clicks the existing opener.
    assert "if(new URLSearchParams(location.search).get('generate')==='notes')" in ready.text
    assert "cleanUrl.searchParams.delete('generate')" in ready.text
    assert "noteDialog?.dataset.noteGenerateEligible==='1'" in ready.text
    # Partial swaps can run deferred workspace.js after this inline script.
    assert "window.localplaudWorkspaceTeardown!==priorWorkspace" in ready.text
    # Partial (progressive) navigation renders the same handoff.
    partial = c.get("/file/ready?tab=notes&generate=notes", headers={"HX-Request": "true"})
    assert partial.status_code == 200 and _eligible(partial.text) == "1"

    for file_id in ("trashed", "cloudonly", "busy"):
        page = c.get(f"/file/{file_id}?tab=notes&generate=notes")
        assert page.status_code == 200
        assert _eligible(page.text) == "0", file_id

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(StageRun)) == before
