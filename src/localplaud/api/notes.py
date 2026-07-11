"""Saved-note API, including promoting grounded Ask answers."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from ..ask_threads import note_to_dict, save_answer_as_note
from ..db.models import UserNote
from ..db.session import session_scope

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
    title: str = Field(min_length=1, max_length=200)
    content_md: str = Field(min_length=1, max_length=200_000)

    @field_validator("title", "content_md")
    @classmethod
    def trim(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value


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
        parts = [f"# {note.title}", "", note.content_md.strip(), ""]
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
        return PlainTextResponse("\n".join(parts), media_type="text/markdown")


@router.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: int) -> Response:
    with session_scope() as session:
        note = session.get(UserNote, note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="note not found")
        session.delete(note)
    return Response(status_code=204)
