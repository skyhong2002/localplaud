"""Deleting an AI-generated note from the recording workspace."""

from __future__ import annotations

from sqlalchemy import select

from tests.test_web_ui import _client, _seed


def _summary_id(template: str = "meeting") -> int:
    from localplaud.db.models import Summary
    from localplaud.db.session import session_scope

    with session_scope() as session:
        return session.scalar(
            select(Summary.id).where(Summary.file_id == "r1", Summary.template == template)
        )


def test_deleting_a_generated_note_keeps_history_copy_and_drops_its_index(monkeypatch, tmp_path):
    from localplaud.db.models import (
        KnowledgeDocument,
        Summary,
        SummaryRevision,
        UserNote,
    )
    from localplaud.db.session import session_scope

    client = _client(monkeypatch, tmp_path)
    _seed()
    summary_id = _summary_id()
    with session_scope() as session:
        session.add(
            UserNote(
                file_id="r1",
                title="My copy",
                content_md="# Sync\n\n- my edit",
                source_type="generated_summary",
                source_summary_id=summary_id,
            )
        )
        session.add(
            KnowledgeDocument(
                kind="generated_summary",
                file_id="r1",
                summary_id=summary_id,
                content_sha256="0" * 64,
                generation="g",
            )
        )
    with session_scope() as session:
        assert session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.summary_id == summary_id)
        )

    page = client.get("/file/r1?workspace=true&tab=notes").text
    assert f'data-summary-delete="{summary_id}" data-has-copy="true"' in page

    response = client.delete(f"/api/files/r1/summaries/{summary_id}")
    assert response.status_code == 204

    with session_scope() as session:
        assert session.get(Summary, summary_id) is None
        archived = session.scalars(
            select(SummaryRevision).where(
                SummaryRevision.file_id == "r1", SummaryRevision.template == "meeting"
            )
        ).all()
        assert [(row.content_md, row.archive_reason) for row in archived] == [
            ("# Sync\n\n- point", "deleted")
        ]
        copy = session.scalar(select(UserNote).where(UserNote.title == "My copy"))
        assert copy.content_md == "# Sync\n\n- my edit"
        assert copy.source_summary_id is None
        assert (
            session.scalar(
                select(KnowledgeDocument).where(KnowledgeDocument.summary_id == summary_id)
            )
            is None
        )

    assert (
        f'data-summary-delete="{summary_id}"'
        not in client.get("/file/r1?workspace=true&tab=notes").text
    )
    assert client.delete(f"/api/files/r1/summaries/{summary_id}").status_code == 404


def test_plaud_notes_and_the_mind_map_cannot_be_deleted(monkeypatch, tmp_path):
    from localplaud.db.models import Summary
    from localplaud.db.session import session_scope

    client = _client(monkeypatch, tmp_path)
    _seed()
    with session_scope() as session:
        session.get(Summary, _summary_id()).source = "cloud"
        session.add(Summary(file_id="r1", template="mind_map", content_md="# Map"))

    page = client.get("/file/r1?workspace=true&tab=notes").text
    assert "data-summary-delete=" not in page

    assert client.delete(f"/api/files/r1/summaries/{_summary_id()}").status_code == 400
    assert client.delete(f"/api/files/r1/summaries/{_summary_id('mind_map')}").status_code == 400
    assert client.delete(f"/api/files/other/summaries/{_summary_id()}").status_code == 404
    with session_scope() as session:
        assert session.scalar(select(Summary).where(Summary.file_id == "r1").limit(1))
