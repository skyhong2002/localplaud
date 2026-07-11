"""Authorized webhook integration CRUD and health API."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..db.session import session_scope
from ..integrations import (
    delete_webhook_integration,
    list_webhook_integrations,
    save_webhook_integration,
    test_webhook_integration,
)

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


class WebhookBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2048)
    secret_ref: str | None = Field(default=None, max_length=256)
    scopes: list[Literal["metadata", "transcript", "notes"]] = Field(
        default_factory=lambda: ["metadata"], min_length=1, max_length=3
    )
    enabled: bool = True
    allow_private_network: bool = False

    @model_validator(mode="after")
    def unique_scopes(self):
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("webhook scopes must be unique")
        return self


@router.get("/webhooks")
def list_webhooks():
    with session_scope() as session:
        return {"webhooks": list_webhook_integrations(session)}


@router.post("/webhooks", status_code=201)
def create_webhook(body: WebhookBody):
    with session_scope() as session:
        try:
            return save_webhook_integration(session, body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/webhooks/{integration_id}")
def update_webhook(integration_id: int, body: WebhookBody):
    with session_scope() as session:
        try:
            return save_webhook_integration(session, body.model_dump(), integration_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/webhooks/{integration_id}/test")
def test_webhook(integration_id: int):
    with session_scope() as session:
        try:
            return test_webhook_integration(session, integration_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/webhooks/{integration_id}", status_code=204)
def delete_webhook(integration_id: int):
    with session_scope() as session:
        try:
            delete_webhook_integration(session, integration_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
