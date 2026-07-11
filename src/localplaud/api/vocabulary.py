"""Custom-vocabulary CRUD and explicit library application."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..db.models import VocabularyTerm
from ..db.session import session_scope
from ..vocabulary import apply_vocabulary_to_library

router = APIRouter(prefix="/api/vocabulary", tags=["vocabulary"])


class VocabularyInput(BaseModel):
    source_text: str = Field(min_length=1, max_length=300)
    replacement_text: str = Field(min_length=1, max_length=300)
    language: str | None = Field(default=None, max_length=24)
    case_sensitive: bool = False
    enabled: bool = True

    @field_validator("source_text", "replacement_text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text cannot be blank")
        return value

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str | None) -> str | None:
        return value.strip().replace("_", "-") if value and value.strip() else None


def _item(row: VocabularyTerm) -> dict:
    return {
        "id": row.id,
        "source_text": row.source_text,
        "replacement_text": row.replacement_text,
        "language": row.language,
        "case_sensitive": row.case_sensitive,
        "enabled": row.enabled,
    }


def _duplicate(session, payload: VocabularyInput, exclude_id: int | None = None) -> bool:
    stmt = select(VocabularyTerm.id).where(VocabularyTerm.source_text == payload.source_text)
    stmt = (
        stmt.where(VocabularyTerm.language == payload.language)
        if payload.language is not None
        else stmt.where(VocabularyTerm.language.is_(None))
    )
    if exclude_id is not None:
        stmt = stmt.where(VocabularyTerm.id != exclude_id)
    return session.scalar(stmt) is not None


@router.get("")
def list_vocabulary() -> dict:
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(VocabularyTerm).order_by(
                    VocabularyTerm.enabled.desc(),
                    func.lower(VocabularyTerm.source_text),
                    VocabularyTerm.id,
                )
            )
        )
        return {"terms": [_item(row) for row in rows]}


@router.post("", status_code=201)
def create_vocabulary(payload: VocabularyInput) -> dict:
    try:
        with session_scope() as session:
            if _duplicate(session, payload):
                raise HTTPException(status_code=409, detail="term already exists for that language")
            row = VocabularyTerm(**payload.model_dump())
            session.add(row)
            session.flush()
            return _item(row)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="term already exists for that language") from exc


@router.put("/{term_id}")
def update_vocabulary(term_id: int, payload: VocabularyInput) -> dict:
    try:
        with session_scope() as session:
            row = session.get(VocabularyTerm, term_id)
            if row is None:
                raise HTTPException(status_code=404, detail="vocabulary term not found")
            if _duplicate(session, payload, exclude_id=term_id):
                raise HTTPException(status_code=409, detail="term already exists for that language")
            for key, value in payload.model_dump().items():
                setattr(row, key, value)
            session.flush()
            return _item(row)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="term already exists for that language") from exc


@router.delete("/{term_id}")
def delete_vocabulary(term_id: int) -> dict:
    with session_scope() as session:
        row = session.get(VocabularyTerm, term_id)
        if row is None:
            raise HTTPException(status_code=404, detail="vocabulary term not found")
        session.delete(row)
    return {"deleted": term_id}


@router.post("/apply-library")
def apply_library_vocabulary() -> dict:
    return apply_vocabulary_to_library()
