"""Manual-note picker, workspace, and export journey coverage."""

from __future__ import annotations

import re
import zipfile
from io import BytesIO

import pytest
from docx import Document
from pypdf import PdfReader


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv(
        "LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'manual-notes-ui.db'}"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)

    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    with TestClient(app) as test_client:
        yield test_client


def _recording(file_id: str, title: str, start: int, *, trash: bool = False):
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            PlaudFile(
                id=file_id,
                filename=title,
                start_time_ms=start,
                status=FileStatus.metadata_only,
                is_trash=trash,
            )
        )


def test_recording_picker_is_bounded_literal_and_excludes_trash(client):
    _recording("older", "Older decision", 1_700_000_000_000)
    _recording("newer", "Newer decision", 1_800_000_000_000)
    _recording("literal", r"Budget 100%_done\\path", 1_750_000_000_000)
    _recording("lookalike", "Budget 100XXdone/path", 1_740_000_000_000)
    _recording("trash", "Newer hidden", 1_900_000_000_000, trash=True)

    response = client.get("/api/files/picker")
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["recordings"]] == [
        "newer",
        "literal",
        "lookalike",
        "older",
    ]
    assert response.json()["recordings"][0]["recorded_at"].endswith("+00:00")
    literal = client.get("/api/files/picker", params={"q": "100%_"}).json()
    assert [row["id"] for row in literal["recordings"]] == ["literal"]
    assert len(client.get("/api/files/picker?limit=2").json()["recordings"]) == 2
    assert client.get("/api/files/picker?limit=0").status_code == 422
    assert client.get("/api/files/picker?limit=51").status_code == 422
    assert client.get("/api/files/picker", params={"q": "x" * 201}).status_code == 422


def test_recording_and_notes_hub_render_manual_lifecycle_contract(client):
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope

    _recording("r1", "Weekly product sync", 1_800_000_000_000)
    with session_scope() as session:
        notes = [
            UserNote(file_id="r1", title="Decision log", content_md="raw **Markdown**", source_type="manual"),
            UserNote(file_id="r1", title="Grounded answer", content_md="Ask body", source_type="ask"),
            UserNote(
                file_id="r1",
                title="Generated copy",
                content_md="Generated body",
                source_type="generated_summary",
            ),
        ]
        session.add_all(notes)
        session.flush()
        manual_id = notes[0].id

    detail = client.get(f"/file/r1?tab=notes&note_id={manual_id}")
    assert detail.status_code == 200
    assert re.search(
        rf'data-note-target="saved-{manual_id}" class="note-tab saved-note-tab on"',
        detail.text,
    )
    assert f'data-note-panel="saved-{manual_id}" hidden' not in detail.text
    for label in ("Created by you", "Saved from Ask", "Editable generated copy"):
        assert label in detail.text
    assert 'id="manual-note-backdrop" data-dirty="false" hidden' in detail.text
    assert "Discard this unfinished note?" in detail.text
    assert "window.localplaudModal" in detail.text
    assert "data-note-copy" in detail.text and "data-note-delete" in detail.text
    assert "textarea[name=\"content_md\"]'" in detail.text
    assert "data-note-add" not in detail.text
    assert f'data-user-note-history="{manual_id}"' in detail.text
    assert 'data-note-version="1"' in detail.text
    assert 'name="base_version" value="1"' in detail.text
    assert detail.text.count('id="user-note-history-backdrop"') == 1
    assert 'class="import-modal user-note-history-drawer"' in detail.text
    assert "window.localplaudModal({backdrop,background" in detail.text
    assert "before_version" in detail.text and "next_before_version" in detail.text
    assert "timeZone:workspaceTimezone" in detail.text
    assert "Discard unsaved changes and open version history?" in detail.text
    assert "This note changed elsewhere. Your text is still here." in detail.text
    assert "event.stopImmediatePropagation();closeConfirmation()" in detail.text
    assert "previewController?.abort();previewController=null" in detail.text
    assert "preview.innerHTML=data.content_html" in detail.text
    assert "cancel.disabled=true;restore.disabled=true" in detail.text
    assert "data.detail?.code==='note_changed'" in detail.text
    assert "Copy draft and reload" in detail.text
    assert "history_restored" in detail.text
    assert "requestAnimationFrame(()=>document.querySelector" in detail.text

    hub = client.get("/notes")
    assert hub.status_code == 200
    assert "Write your own notes, edit generated copies" in hub.text
    assert 'id="manual-note-recording-search"' in hub.text
    assert "Search recordings…" in hub.text
    assert "Library · ask" not in hub.text
    for label in ("Created by you", "Saved from Ask", "Editable generated copy"):
        assert label in hub.text
    assert "area.value=text" in hub.text and "document.execCommand('copy')" in hub.text
    assert "deleteMessages={manual:" in hub.text
    assert "clearTimeout(searchTimer)" in hub.text
    assert "if(cleanupController.signal.aborted)return" in hub.text
    assert "controller===pickerController" in hub.text
    assert "pickerController?.abort();pickerController=null;results.replaceChildren()" in hub.text
    assert "function renderRecordings(data){selectedFileId=null;create.disabled=true" in hub.text
    assert "modal.setBusy(true)" in hub.text
    assert "closeButton.disabled=true;cancelButton.disabled=true" in hub.text
    assert "backdrop.dataset.busy==='true'" in hub.text
    assert "manualNoteModal.setBusy(true)" in detail.text
    assert "if(error.name!=='AbortError')status.textContent" in detail.text
    assert f'data-user-note-history="{manual_id}"' in hub.text
    assert hub.text.count('id="user-note-history-backdrop"') == 1
    assert ".user-note-history-preview { max-width:100%;max-height:280px;overflow:auto" in hub.text
    assert ".user-note-history-title { overflow-wrap:anywhere" in hub.text
    assert ".user-note-history-snippet { display:-webkit-box" in hub.text
    assert ".user-note-history-footer [role=\"status\"] { min-width:0;flex:1 1 180px;overflow-wrap:anywhere; }" in hub.text


def test_notes_hub_without_recordings_guides_to_add_audio(client):
    page = client.get("/notes")
    assert page.status_code == 200
    assert "Add a recording before creating a note." in page.text
    assert 'data-open-import="device"' in page.text
    assert 'id="manual-note-backdrop"' not in page.text


def test_manual_note_create_update_search_and_recording_exports(client):
    _recording("journey", "Planning session", 1_800_000_000_000)
    created = client.post(
        "/api/files/journey/notes",
        json={
            "title": "Decision log",
            "content_md": "## Follow-up\n\n| Owner | Action |\n| --- | --- |\n| Sky | Ship beta |",
        },
    )
    assert created.status_code == 201
    note_id = created.json()["id"]

    selected = client.get(f"/file/journey?tab=notes&note_id={note_id}")
    assert f'data-note-target="saved-{note_id}" class="note-tab saved-note-tab on"' in selected.text
    raw_body = "    UniqueManualNeedle\n\n- Ship beta  \n"
    updated = client.put(
        f"/api/notes/{note_id}",
        json={"title": "Release decision", "content_md": raw_body, "base_version": 1},
    )
    assert updated.status_code == 200
    search = client.get("/search", params={"q": "UniqueManualNeedle"})
    assert search.status_code == 200
    assert "Release decision" in search.text and "Planning session" in search.text

    markdown = client.get(f"/api/notes/{note_id}/export.md")
    assert markdown.status_code == 200
    assert "# Release decision" in markdown.text and raw_body in markdown.text
    assert markdown.headers["content-disposition"] == f'attachment; filename="note-{note_id}.md"'
    recording_markdown = client.get("/file/journey/export/notes.md")
    archive_markdown = client.get("/file/journey/export.md")
    assert recording_markdown.status_code == archive_markdown.status_code == 200
    assert raw_body in recording_markdown.text and raw_body in archive_markdown.text
    bulk = client.post(
        "/api/files/export",
        json={"file_ids": ["journey"], "notes_format": "md"},
    )
    assert bulk.status_code == 200
    with zipfile.ZipFile(BytesIO(bulk.content)) as archive:
        notes_name = next(name for name in archive.namelist() if name.endswith("/notes.md"))
        assert raw_body in archive.read(notes_name).decode()
    txt = client.get("/file/journey/export/notes.txt")
    docx = client.get("/file/journey/export/notes.docx")
    pdf = client.get("/file/journey/export/notes.pdf")
    assert txt.status_code == docx.status_code == pdf.status_code == 200
    assert b"UniqueManualNeedle" in txt.content
    assert any("UniqueManualNeedle" in paragraph.text for paragraph in Document(BytesIO(docx.content)).paragraphs)
    assert len(PdfReader(BytesIO(pdf.content)).pages) >= 1
