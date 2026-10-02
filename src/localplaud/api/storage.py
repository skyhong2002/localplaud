"""Storage use and derived-data retention APIs."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..db.session import session_scope
from ..storage_usage import (
    MAX_BACKUP_KEEP,
    apply_retention,
    get_retention_policy,
    retention_plan,
    save_retention_policy,
    storage_usage,
)

router = APIRouter(prefix="/api/storage", tags=["storage"])


class RetentionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backup_keep_latest: int | None = Field(default=None, ge=1, le=MAX_BACKUP_KEEP)


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The exact archives the user confirmed in the dialog; anything else is kept.
    confirm_names: list[str] = Field(default_factory=list, max_length=MAX_BACKUP_KEEP)


@router.get("")
def usage() -> dict:
    with session_scope() as session:
        return {"usage": storage_usage(session), "retention": get_retention_policy(session)}


@router.put("/retention")
def update_retention(body: RetentionBody) -> dict:
    with session_scope() as session:
        try:
            save_retention_policy(session, body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return retention_plan(session)


@router.get("/retention/plan")
def plan(backup_keep_latest: int | None = None, proposed: bool = False) -> dict:
    """Preview what the saved policy, or a proposed one, would remove."""
    if backup_keep_latest is not None and not 1 <= backup_keep_latest <= MAX_BACKUP_KEEP:
        raise HTTPException(status_code=422, detail="backup_keep_latest out of range")
    with session_scope() as session:
        return retention_plan(
            session, {"backup_keep_latest": backup_keep_latest} if proposed else None
        )


@router.post("/retention/apply")
def apply(body: ApplyBody) -> dict:
    with session_scope() as session:
        return apply_retention(session, body.confirm_names)
