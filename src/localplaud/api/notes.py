"""Saved-note API, including promoting grounded Ask answers."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..ask_threads import note_to_dict, save_answer_as_note
from ..db.models import PlaudFile, Summary, UserNote
from ..db.session import session_scope
from ..note_history import source_summary_provenance

router = APIRouter(prefix="/api", tags=["notes"])


class SaveAnswerBody(BaseModel):
    title: str | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def trim_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    content_md: str = Field(min_length=1, max_length=200_000)

    @field_validator("title", mode="before")
    @classmethod
    def trim_title(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("content_md")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


def _serialize_manual_note_creation(session: Session) -> None:
    # Reserve the SQLite writer before checking the trash boundary so a concurrent
    # mirror update cannot commit between that check and the note insert.
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


@router.get("/notes")
def list_notes(file_id: str | None = None) -> dict:
    with session_scope() as session:
        stmt = select(UserNote).order_by(UserNote.updated_at.desc(), UserNote.id.desc())
        if file_id is not None:
            stmt = stmt.where(UserNote.file_id == file_id)
        return {"notes": [note_to_dict(row) for row in session.scalars(stmt)]}


@router.post("/ask/messages/{message_id}/save-note", status_code=201)
def save_answer(message_id: int, body: SaveAnswerBody) -> dict:
    try:
        return save_answer_as_note(message_id, body.title)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/files/{file_id}/notes", status_code=201)
def create_manual_note(file_id: str, body: NoteBody) -> dict:
    with session_scope() as session:
        _serialize_manual_note_creation(session)
        recording = session.get(PlaudFile, file_id)
        if recording is None:
            raise HTTPException(status_code=404, detail="recording not found")
        if recording.is_trash:
            raise HTTPException(status_code=409, detail="recording is in trash")
        note = UserNote(
            file_id=file_id,
            title=body.title,
            content_md=body.content_md,
            source_type="manual",
            ask_message_id=None,
            source_summary_id=None,
            source_summary_snapshot=None,
            citations=[],
        )
        session.add(note)
        session.flush()
        return note_to_dict(note)


@router.post("/files/{file_id}/summaries/{summary_id}/editable-copy", status_code=201)
def copy_generated_summary(file_id: str, summary_id: int) -> dict:
    """Create one editable note without mutating the generated artifact."""
    with session_scope() as session:
        summary = session.get(Summary, summary_id)
        if summary is None or summary.file_id != file_id:
            raise HTTPException(status_code=404, detail="generated note not found")
        if summary.template == "mind_map":
            raise HTTPException(status_code=409, detail="mind maps are not editable notes")
        existing = session.scalar(
            select(UserNote).where(UserNote.source_summary_id == summary.id)
        )
        if existing is not None:
            return note_to_dict(existing)
        note = UserNote(
            file_id=file_id,
            title=(summary.title or summary.template.replace("-", " ").title())[:200],
            content_md=summary.content_md,
            source_type="generated_summary",
            source_summary_id=summary.id,
            # The id tracks the live output slot, which a history restore
            # rewrites in place; the snapshot pins the exact copied version.
            source_summary_snapshot=source_summary_provenance(summary),
            citations=[],
        )
        session.add(note)
        session.flush()
        return note_to_dict(note)


@router.put("/notes/{note_id}")
def update_note(note_id: int, body: NoteBody) -> dict:
    with session_scope() as session:
        note = session.get(UserNote, note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="note not found")
        note.title = body.title
        note.content_md = body.content_md
        session.flush()
        return note_to_dict(note)


@router.get("/notes/{note_id}/export.md", response_class=PlainTextResponse)
def export_note(note_id: int) -> PlainTextResponse:
    with session_scope() as session:
        note = session.get(UserNote, note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="note not found")
        parts = [f"# {note.title}", "", note.content_md, ""]
        if note.citations:
            parts.extend(["## Sources", ""])
            for citation in note.citations:
                label = citation.get("filename") or citation.get("file_id") or "Recording"
                start = citation.get("start")
                if start is not None:
                    minutes, seconds = divmod(max(0, int(start)), 60)
                    label += f" @ {minutes:02d}:{seconds:02d}"
                parts.append(f"- {label}")
            parts.append("")
        return PlainTextResponse(
            "\n".join(parts),
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="note-{note.id}.md"'},
        )


@router.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: int) -> Response:
    with session_scope() as session:
        note = session.get(UserNote, note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="note not found")
        session.delete(note)
    return Response(status_code=204)
