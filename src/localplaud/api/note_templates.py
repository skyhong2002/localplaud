"""Versioned note-template catalog and per-recording selection API."""

from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update

from ..db.models import (
    ExecutionProfile,
    NoteTemplate,
    PlaudFile,
    StageName,
    StageRun,
    StageStatus,
)
from ..db.session import session_scope
from ..worker.pipeline import processing_claim_active

router = APIRouter(prefix="/api", tags=["note-templates"])
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_CATALOG = {
    "default": {"category": "General", "scenario": "Any recording", "description": "Balanced summary, key points, and action items.", "author": "localplaud", "popularity": 100},
    "meeting": {"category": "Work", "scenario": "Meetings", "description": "Decisions, owners, action items, and unresolved questions.", "author": "localplaud", "popularity": 96},
    "call": {"category": "Work", "scenario": "Calls", "description": "Purpose, commitments, sentiment, and follow-ups from a call.", "author": "localplaud", "popularity": 84},
    "lecture": {"category": "Education", "scenario": "Lectures", "description": "Concept-focused study notes with review questions.", "author": "localplaud", "popularity": 91},
    "personal": {"category": "Personal", "scenario": "Voice memos", "description": "A concise TL;DR, highlights, and personal to-dos.", "author": "localplaud", "popularity": 79},
}


class TemplateBody(BaseModel):
    key: str | None = None
    name: str = Field(min_length=1, max_length=80)
    system_prompt: str = Field(min_length=1, max_length=20_000)
    instructions: str = Field(min_length=1, max_length=20_000)
    execution_profile_id: int | None = None

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


class CopyTemplateBody(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        value = value.strip().lower()
        if not _KEY.fullmatch(value):
            raise ValueError("key must contain lowercase letters, numbers, and hyphens")
        return value


def _item(row: NoteTemplate) -> dict:
    catalog = _CATALOG.get(row.key, {})
    return {
        "id": row.id,
        "key": row.key,
        "version": row.version,
        "name": row.name,
        "system_prompt": row.system_prompt,
        "instructions": row.instructions,
        "is_builtin": row.is_builtin,
        "is_active": row.is_active,
        "category": row.category or catalog.get("category", "Custom"),
        "scenario": row.scenario or catalog.get("scenario", "Workspace"),
        "description": row.description or catalog.get(
            "description", next((line.strip("# ") for line in row.instructions.splitlines() if line.strip()), "Custom structured notes")
        ),
        "author": row.author or catalog.get("author", "Local workspace"),
        "popularity": row.popularity if row.popularity is not None else catalog.get("popularity"),
        "provenance": row.provenance or ("first-party" if row.is_builtin else "personal"),
        "execution_profile_id": row.execution_profile_id,
    }


def _validate_profile(session, profile_id: int | None) -> None:
    if profile_id is not None and session.get(ExecutionProfile, profile_id) is None:
        raise HTTPException(status_code=404, detail="execution profile not found")


@router.get("/note-templates")
def list_note_templates(include_history: bool = False) -> dict:
    with session_scope() as session:
        stmt = select(NoteTemplate).order_by(NoteTemplate.name, NoteTemplate.version.desc())
        if not include_history:
            stmt = stmt.where(NoteTemplate.is_active.is_(True))
        return {"templates": [_item(row) for row in session.scalars(stmt)]}


@router.post("/note-templates/{key}/copy", status_code=201)
def copy_note_template(key: str, body: CopyTemplateBody) -> dict:
    """Copy an active template into an independently versioned personal template."""
    with session_scope() as session:
        source = session.scalar(
            select(NoteTemplate).where(
                NoteTemplate.key == key, NoteTemplate.is_active.is_(True)
            )
        )
        if source is None:
            raise HTTPException(status_code=404, detail="template not found")
        if session.scalar(select(NoteTemplate.id).where(NoteTemplate.key == body.key)):
            raise HTTPException(status_code=409, detail="template key already exists")
        row = NoteTemplate(
            key=body.key,
            version=1,
            name=body.name or f"{source.name} copy",
            system_prompt=source.system_prompt,
            instructions=source.instructions,
            category=_item(source)["category"],
            scenario=_item(source)["scenario"],
            description=_item(source)["description"],
            author="Local workspace",
            provenance="personal-copy",
            popularity=None,
            execution_profile_id=source.execution_profile_id,
            is_builtin=False,
            is_active=True,
        )
        session.add(row)
        session.flush()
        return _item(row)


@router.post("/note-templates", status_code=201)
def create_note_template(body: TemplateBody) -> dict:
    if body.key is None:
        raise HTTPException(status_code=422, detail="key is required")
    with session_scope() as session:
        _validate_profile(session, body.execution_profile_id)
        exists = session.scalar(select(NoteTemplate.id).where(NoteTemplate.key == body.key))
        if exists is not None:
            raise HTTPException(status_code=409, detail="template key already exists")
        row = NoteTemplate(
            key=body.key,
            version=1,
            name=body.name,
            system_prompt=body.system_prompt,
            instructions=body.instructions,
            category="Custom",
            scenario="Workspace",
            author="Local workspace",
            provenance="personal",
            execution_profile_id=body.execution_profile_id,
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
        profile_id = (
            body.execution_profile_id
            if "execution_profile_id" in body.model_fields_set
            else current.execution_profile_id
        )
        _validate_profile(session, profile_id)
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
            category=current.category,
            scenario=current.scenario,
            description=current.description,
            author=current.author,
            provenance=current.provenance,
            popularity=current.popularity,
            execution_profile_id=profile_id,
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
        if processing_claim_active(recording):
            raise HTTPException(
                status_code=409,
                detail="recording is processing; change template when it finishes",
            )
        if body.key is not None and body.key != "auto":
            exists = session.scalar(
                select(NoteTemplate.id).where(
                    NoteTemplate.key == body.key, NoteTemplate.is_active.is_(True)
                )
            )
            if exists is None:
                raise HTTPException(status_code=404, detail="template not found")
        recording.note_template_key = body.key
        stale_generation = secrets.token_hex(16)
        for stage in (StageName.summarize, StageName.mind_map, StageName.index):
            run = session.scalar(
                select(StageRun).where(StageRun.file_id == file_id, StageRun.stage == stage)
            )
            if run is not None:
                run.status = StageStatus.pending
                run.detail = (run.detail or {}) | {
                    "stale": True,
                    "stale_generation": stale_generation,
                    "reason": "note template changed",
                }
                run.error = None
    return {"file_id": file_id, "key": body.key}


@router.get("/files/{file_id}/note-template/recommendation")
def recording_note_template_recommendation(file_id: str) -> dict:
    from ..config import get_settings
    from ..template_auto import recommend_template
    from ..worker.pipeline import _load_transcript

    with session_scope() as session:
        recording = session.get(PlaudFile, file_id)
        if recording is None:
            raise HTTPException(status_code=404, detail="recording not found")
        title, duration_ms = recording.display_title, recording.duration_ms
    loaded = _load_transcript(file_id, get_settings())
    transcript_text = loaded[0].text if loaded is not None else ""
    recommendation = recommend_template(
        title=title, transcript=transcript_text, duration_ms=duration_ms
    )
    with session_scope() as session:
        template = session.scalar(
            select(NoteTemplate).where(
                NoteTemplate.key == recommendation["key"],
                NoteTemplate.is_active.is_(True),
            )
        )
        recommendation["template"] = _item(template) if template is not None else None
    return recommendation
