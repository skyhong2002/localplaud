"""Saved-note API, including promoting grounded Ask answers."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from ..ask_threads import note_to_dict, save_answer_as_note
from ..db.models import PlaudFile, Summary, UserNote, UserNoteRevision
from ..db.session import session_scope
from ..editable_notes import (
    USER_NOTE_CONTENT_MAX_LENGTH,
    USER_NOTE_TITLE_MAX_LENGTH,
    EditableNoteContentError,
    editable_note_preview,
    require_editable_note_content,
)
from ..markdown import render_markdown
from ..note_history import source_summary_provenance

router = APIRouter(prefix="/api", tags=["notes"])


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class SaveAnswerBody(BaseModel):
    title: str | None = Field(default=None, max_length=USER_NOTE_TITLE_MAX_LENGTH)

    @field_validator("title")
    @classmethod
    def trim_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=USER_NOTE_TITLE_MAX_LENGTH)
    content_md: str = Field(min_length=1, max_length=USER_NOTE_CONTENT_MAX_LENGTH)

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


class NoteUpdateBody(NoteBody):
    base_version: int = Field(ge=1)


class NoteRestoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(ge=1)


def _serialize_manual_note_creation(session: Session) -> None:
    # Reserve the SQLite writer before checking the trash boundary so a concurrent
    # mirror update cannot commit between that check and the note insert.
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def _lock_note_for_write(session: Session, note_id: int) -> UserNote | None:
    """Serialize the version check with the archive and live-row update."""
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
        return session.get(UserNote, note_id)
    return session.scalar(
        select(UserNote).where(UserNote.id == note_id).with_for_update()
    )


def _archive_live_note(session: Session, note: UserNote) -> None:
    session.add(
        UserNoteRevision(
            note_id=note.id,
            version=note.version,
            title=note.title,
            content_md=note.content_md,
            content_preview=editable_note_preview(note.content_md),
        )
    )


def _require_current_version(note: UserNote, base_version: int, *, action: str) -> None:
    if note.version != base_version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "note_changed",
                "message": f"note changed; reload before {action}",
                "current_version": note.version,
            },
        )


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
    except EditableNoteContentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
        try:
            require_editable_note_content(summary.content_md)
        except EditableNoteContentError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
def update_note(note_id: int, body: NoteUpdateBody) -> dict:
    with session_scope() as session:
        note = _lock_note_for_write(session, note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="note not found")
        _require_current_version(note, body.base_version, action="saving")
        if note.title == body.title and note.content_md == body.content_md:
            return note_to_dict(note)
        _archive_live_note(session, note)
        note.title = body.title
        note.content_md = body.content_md
        note.version += 1
        session.flush()
        return note_to_dict(note)


@router.get("/notes/{note_id}/history")
def list_note_history(
    note_id: int,
    limit: int = Query(default=20, ge=1, le=50),
    before_version: int | None = Query(default=None, ge=1),
) -> dict:
    """Return one bounded metadata page without loading archived Markdown."""
    with session_scope() as session:
        if session.scalar(select(UserNote.id).where(UserNote.id == note_id)) is None:
            raise HTTPException(status_code=404, detail="note not found")
        stmt = (
            select(
                UserNoteRevision.id,
                UserNoteRevision.version,
                UserNoteRevision.title,
                UserNoteRevision.content_preview,
                UserNoteRevision.archived_at,
            )
            .where(UserNoteRevision.note_id == note_id)
            .order_by(UserNoteRevision.version.desc())
            .limit(limit + 1)
        )
        if before_version is not None:
            stmt = stmt.where(UserNoteRevision.version < before_version)
        rows = session.execute(stmt).mappings().all()
        has_more = len(rows) > limit
        page = rows[:limit]
        return {
            "items": [
                dict(row) | {"archived_at": _utc_iso(row["archived_at"])}
                for row in page
            ],
            "next_before_version": page[-1]["version"] if has_more and page else None,
        }


@router.get("/notes/{note_id}/history/{version}")
def get_note_revision(note_id: int, version: int) -> dict:
    with session_scope() as session:
        if session.scalar(select(UserNote.id).where(UserNote.id == note_id)) is None:
            raise HTTPException(status_code=404, detail="note not found")
        revision = session.scalar(
            select(UserNoteRevision).where(
                UserNoteRevision.note_id == note_id,
                UserNoteRevision.version == version,
            )
        )
        if revision is None:
            raise HTTPException(status_code=404, detail="note revision not found")
        return {
            "id": revision.id,
            "note_id": revision.note_id,
            "version": revision.version,
            "title": revision.title,
            "content_md": revision.content_md,
            "content_html": str(render_markdown(revision.content_md)),
            "archived_at": _utc_iso(revision.archived_at),
        }


@router.post("/notes/{note_id}/history/{version}/restore")
def restore_note_revision(note_id: int, version: int, body: NoteRestoreBody) -> dict:
    with session_scope() as session:
        note = _lock_note_for_write(session, note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="note not found")
        _require_current_version(note, body.base_version, action="restoring")
        revision = session.scalar(
            select(UserNoteRevision).where(
                UserNoteRevision.note_id == note_id,
                UserNoteRevision.version == version,
            )
        )
        if revision is None:
            raise HTTPException(status_code=404, detail="note revision not found")
        _archive_live_note(session, note)
        note.title = revision.title
        note.content_md = revision.content_md
        note.version += 1
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
        # Keep deletion bounded even when every archived body is near the
        # 200k limit; ORM delete-orphan would otherwise materialize them all.
        session.execute(
            delete(UserNoteRevision).where(UserNoteRevision.note_id == note_id)
        )
        session.delete(note)
    return Response(status_code=204)
