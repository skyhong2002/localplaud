"""Versioned note-template catalog and per-recording selection API."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update

from ..db.models import NoteTemplate, PlaudFile, StageName, StageRun, StageStatus
from ..db.session import session_scope

router = APIRouter(prefix="/api", tags=["note-templates"])
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class TemplateBody(BaseModel):
    key: str | None = None
    name: str = Field(min_length=1, max_length=80)
    system_prompt: str = Field(min_length=1, max_length=20_000)
    instructions: str = Field(min_length=1, max_length=20_000)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not _KEY.fullmatch(value):
            raise ValueError("key must contain lowercase letters, numbers, and hyphens")
        return value

    @field_validator("name", "system_prompt", "instructions")
    @classmethod
    def trim_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value


class RecordingTemplateBody(BaseModel):
    key: str | None = None


def _item(row: NoteTemplate) -> dict:
    return {
        "id": row.id,
        "key": row.key,
        "version": row.version,
        "name": row.name,
        "system_prompt": row.system_prompt,
        "instructions": row.instructions,
        "is_builtin": row.is_builtin,
        "is_active": row.is_active,
    }


@router.get("/note-templates")
def list_note_templates(include_history: bool = False) -> dict:
    with session_scope() as session:
        stmt = select(NoteTemplate).order_by(NoteTemplate.name, NoteTemplate.version.desc())
        if not include_history:
            stmt = stmt.where(NoteTemplate.is_active.is_(True))
        return {"templates": [_item(row) for row in session.scalars(stmt)]}


@router.post("/note-templates", status_code=201)
def create_note_template(body: TemplateBody) -> dict:
    if body.key is None:
        raise HTTPException(status_code=422, detail="key is required")
    with session_scope() as session:
        exists = session.scalar(select(NoteTemplate.id).where(NoteTemplate.key == body.key))
        if exists is not None:
            raise HTTPException(status_code=409, detail="template key already exists")
        row = NoteTemplate(
            key=body.key,
            version=1,
            name=body.name,
            system_prompt=body.system_prompt,
            instructions=body.instructions,
            is_builtin=False,
            is_active=True,
        )
        session.add(row)
        session.flush()
        return _item(row)


@router.put("/note-templates/{key}", status_code=201)
def create_note_template_version(key: str, body: TemplateBody) -> dict:
    with session_scope() as session:
        current = session.scalar(
            select(NoteTemplate)
            .where(NoteTemplate.key == key, NoteTemplate.is_active.is_(True))
            .order_by(NoteTemplate.version.desc())
        )
        if current is None:
            raise HTTPException(status_code=404, detail="template not found")
        version = (session.scalar(select(func.max(NoteTemplate.version)).where(NoteTemplate.key == key)) or 0) + 1
        session.execute(
            update(NoteTemplate).where(NoteTemplate.key == key).values(is_active=False)
        )
        row = NoteTemplate(
            key=key,
            version=version,
            name=body.name,
            system_prompt=body.system_prompt,
            instructions=body.instructions,
            is_builtin=current.is_builtin,
            is_active=True,
        )
        session.add(row)
        session.flush()
        return _item(row)


@router.delete("/note-templates/{key}")
def archive_note_template(key: str) -> dict:
    if key == "default":
        raise HTTPException(status_code=409, detail="the default template cannot be archived")
    with session_scope() as session:
        current = session.scalar(
            select(NoteTemplate).where(
                NoteTemplate.key == key, NoteTemplate.is_active.is_(True)
            )
        )
        if current is None:
            raise HTTPException(status_code=404, detail="template not found")
        current.is_active = False
        session.execute(
            update(PlaudFile)
            .where(PlaudFile.note_template_key == key)
            .values(note_template_key=None)
        )
    return {"archived": True}


@router.put("/files/{file_id}/note-template")
def select_recording_note_template(file_id: str, body: RecordingTemplateBody) -> dict:
    with session_scope() as session:
        recording = session.get(PlaudFile, file_id)
        if recording is None:
            raise HTTPException(status_code=404, detail="recording not found")
        if body.key is not None:
            exists = session.scalar(
                select(NoteTemplate.id).where(
                    NoteTemplate.key == body.key, NoteTemplate.is_active.is_(True)
                )
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="template not found")
        recording.note_template_key = body.key
        for stage in (StageName.summarize, StageName.mind_map):
            run = session.scalar(
                select(StageRun).where(StageRun.file_id == file_id, StageRun.stage == stage)
            )
            if run is not None:
                run.status = StageStatus.pending
                run.detail = (run.detail or {}) | {"stale": True, "reason": "note template changed"}
                run.error = None
    return {"file_id": file_id, "key": body.key}
