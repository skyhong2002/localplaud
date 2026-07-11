"""Headless provider/profile APIs; intentionally no settings UI."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db.session import session_scope
from ..providers.service import (
    list_connections,
    list_models,
    list_profiles,
    preview_resolution,
    select_recording_override,
)

router = APIRouter(prefix="/api/providers", tags=["providers"])


class PreviewRequest(BaseModel):
    rule_or_folder: dict | None = None
    template: dict | None = None
    recording: dict | None = None


class OverrideRequest(BaseModel):
    profile_id: int
    stages: dict = Field(default_factory=dict)
    policy: dict = Field(default_factory=dict)


@router.get("/connections")
def connections():
    with session_scope() as session:
        return {"connections": list_connections(session)}


@router.get("/models")
def models():
    with session_scope() as session:
        return {"models": list_models(session)}


@router.get("/profiles")
def profiles():
    with session_scope() as session:
        return {"profiles": list_profiles(session)}


@router.post("/resolve")
def resolve(body: PreviewRequest):
    with session_scope() as session:
        try:
            result = preview_resolution(
                session, body.rule_or_folder, body.template, body.recording
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"resolved": result.to_dict()}


@router.put("/recordings/{file_id}/override")
def recording_override(file_id: str, body: OverrideRequest):
    with session_scope() as session:
        try:
            return select_recording_override(
                session, file_id, body.profile_id, stages=body.stages, policy=body.policy
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
