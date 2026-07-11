"""AutoFlow rule CRUD, dry-run, execution, and history API."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, select, update

from ..automations import (
    deliver_automation_export,
    deliver_local_notification,
    evaluate_library,
    evaluate_recording,
    match_rule,
    rule_sentence,
    validate_rule_references,
)
from ..config import get_settings
from ..db.models import (
    AutomationExport,
    AutomationRule,
    AutomationRun,
    Notification,
    PlaudFile,
)
from ..db.session import session_scope

router = APIRouter(prefix="/api/automations", tags=["automations"])


class TriggerBody(BaseModel):
    origin: Literal["plaud", "local"] | None = None
    title_contains: str | None = Field(default=None, max_length=200)
    min_duration_minutes: float | None = Field(default=None, ge=0, le=24 * 60)
    max_duration_minutes: float | None = Field(default=None, ge=0, le=24 * 60)
    folder_id: int | None = Field(default=None, gt=0)
    tag_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def duration_order(self):
        if (
            self.min_duration_minutes is not None
            and self.max_duration_minutes is not None
            and self.min_duration_minutes > self.max_duration_minutes
        ):
            raise ValueError("minimum duration cannot exceed maximum duration")
        return self


class ActionsBody(BaseModel):
    note_template_key: str | None = Field(default=None, max_length=64)
    profile_id: int | None = Field(default=None, gt=0)
    folder_id: int | None = Field(default=None, gt=0)
    add_tag_ids: list[int] = Field(default_factory=list, max_length=20)
    export_formats: list[Literal["txt", "srt", "vtt"]] = Field(
        default_factory=list, max_length=3
    )

    @model_validator(mode="after")
    def has_action(self):
        if len(set(self.export_formats)) != len(self.export_formats):
            raise ValueError("export formats must be unique")
        if not any(
            [
                self.note_template_key,
                self.profile_id,
                self.folder_id,
                self.add_tag_ids,
                self.export_formats,
            ]
        ):
            raise ValueError("at least one action is required")
        return self


class RuleBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=10_000)
    trigger: TriggerBody = Field(default_factory=TriggerBody)
    actions: ActionsBody
    notify: bool = False


def _serialize_rule(row: AutomationRule, run_count: int = 0, last_run=None) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "enabled": row.enabled,
        "priority": row.priority,
        "version": row.version,
        "trigger": row.trigger or {},
        "actions": row.actions or {},
        "notify": row.notify,
        "sentence": rule_sentence(row),
        "run_count": run_count,
        "last_run": last_run,
    }


@router.get("/rules")
def list_rules() -> dict:
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(AutomationRule).order_by(AutomationRule.priority, AutomationRule.id)
            )
        )
        output = []
        for row in rows:
            runs = list(
                session.scalars(
                    select(AutomationRun)
                    .where(AutomationRun.rule_id == row.id)
                    .order_by(AutomationRun.created_at.desc())
                )
            )
            last = runs[0] if runs else None
            output.append(
                _serialize_rule(
                    row,
                    len(runs),
                    {
                        "status": last.status,
                        "file_id": last.file_id,
                        "created_at": last.created_at.isoformat(),
                    }
                    if last
                    else None,
                )
            )
        return {"rules": output}


@router.post("/rules", status_code=201)
def create_rule(body: RuleBody) -> dict:
    trigger, actions = body.trigger.model_dump(exclude_none=True), body.actions.model_dump(exclude_none=True)
    try:
        validate_rule_references(trigger, actions)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with session_scope() as session:
        row = AutomationRule(
            name=body.name.strip(), enabled=body.enabled, priority=body.priority,
            trigger=trigger, actions=actions, notify=body.notify,
        )
        session.add(row)
        session.flush()
        return _serialize_rule(row)


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleBody) -> dict:
    trigger, actions = body.trigger.model_dump(exclude_none=True), body.actions.model_dump(exclude_none=True)
    try:
        validate_rule_references(trigger, actions)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with session_scope() as session:
        row = session.get(AutomationRule, rule_id)
        if row is None:
            raise HTTPException(status_code=404, detail="rule not found")
        row.name, row.enabled, row.priority = body.name.strip(), body.enabled, body.priority
        row.trigger, row.actions, row.notify = trigger, actions, body.notify
        row.version += 1
        session.flush()
        return _serialize_rule(row)


@router.post("/rules/{rule_id}/toggle")
def toggle_rule(rule_id: int) -> dict:
    with session_scope() as session:
        row = session.get(AutomationRule, rule_id)
        if row is None:
            raise HTTPException(status_code=404, detail="rule not found")
        row.enabled = not row.enabled
        return {"id": row.id, "enabled": row.enabled}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int) -> dict:
    with session_scope() as session:
        row = session.get(AutomationRule, rule_id)
        if row is None:
            raise HTTPException(status_code=404, detail="rule not found")
        run_ids = select(AutomationRun.id).where(AutomationRun.rule_id == rule_id)
        session.execute(
            update(AutomationExport)
            .where(AutomationExport.automation_run_id.in_(run_ids))
            .values(automation_run_id=None)
        )
        session.execute(
            update(Notification)
            .where(Notification.automation_run_id.in_(run_ids))
            .values(automation_run_id=None)
        )
        session.execute(delete(AutomationRun).where(AutomationRun.rule_id == rule_id))
        session.delete(row)
    return {"deleted": True}


@router.post("/rules/{rule_id}/dry-run")
def dry_run_rule(rule_id: int, limit: int = 100) -> dict:
    with session_scope() as session:
        rule = session.get(AutomationRule, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="rule not found")
        recordings = list(session.scalars(select(PlaudFile).order_by(PlaudFile.start_time_ms.desc())))
        matches = []
        for recording in recordings:
            matched, reasons = match_rule(rule, recording)
            if matched:
                matches.append({"file_id": recording.id, "filename": recording.display_title, "reasons": reasons})
            if len(matches) >= min(max(limit, 1), 500):
                break
        return {"rule_id": rule_id, "matches": matches, "count": len(matches), "mutated": False}


@router.post("/run")
def run_automations_now(limit: int | None = None) -> dict:
    return {"recordings_changed": evaluate_library(limit), "status": "completed"}


@router.get("/runs")
def list_runs(rule_id: int | None = None, limit: int = 100) -> dict:
    with session_scope() as session:
        stmt = select(AutomationRun).order_by(AutomationRun.created_at.desc()).limit(min(max(limit, 1), 500))
        if rule_id is not None:
            stmt = stmt.where(AutomationRun.rule_id == rule_id)
        rows = list(session.scalars(stmt))
        output = []
        for row in rows:
            exports = list(
                session.scalars(
                    select(AutomationExport)
                    .where(AutomationExport.automation_run_id == row.id)
                    .order_by(AutomationExport.format)
                )
            )
            output.append(
                {
                    "id": row.id,
                    "rule_id": row.rule_id,
                    "rule_version": row.rule_version,
                    "file_id": row.file_id,
                    "status": row.status,
                    "matched": row.matched,
                    "detail": row.detail or {},
                    "error": row.error,
                    "created_at": row.created_at.isoformat(),
                    "exports": [_serialize_export(item) for item in exports],
                }
            )
        return {"runs": output}


@router.post("/runs/{run_id}/retry")
def retry_run(run_id: int) -> dict:
    with session_scope() as session:
        run = session.get(AutomationRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run.status != "failed":
            raise HTTPException(status_code=409, detail="only failed runs can be retried")
        file_id = run.file_id
        session.delete(run)
    return {"results": evaluate_recording(file_id)}


def _serialize_notification(row: Notification) -> dict:
    return {
        "id": row.id,
        "automation_run_id": row.automation_run_id,
        "file_id": row.file_id,
        "title": row.title,
        "body": row.body,
        "detail": row.detail or {},
        "read_at": row.read_at.isoformat() if row.read_at else None,
        "dismissed_at": row.dismissed_at.isoformat() if row.dismissed_at else None,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/notifications")
def list_notifications(unread_only: bool = False, limit: int = 100) -> dict:
    with session_scope() as session:
        stmt = (
            select(Notification)
            .where(Notification.dismissed_at.is_(None))
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(min(max(limit, 1), 500))
        )
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        rows = list(session.scalars(stmt))
        return {"notifications": [_serialize_notification(row) for row in rows]}


@router.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int, read: bool = True) -> dict:
    from datetime import UTC, datetime

    with session_scope() as session:
        row = session.get(Notification, notification_id)
        if row is None or row.dismissed_at is not None:
            raise HTTPException(status_code=404, detail="notification not found")
        row.read_at = datetime.now(UTC) if read else None
        return _serialize_notification(row)


@router.post("/notifications/read-all")
def mark_all_notifications_read() -> dict:
    from datetime import UTC, datetime

    with session_scope() as session:
        result = session.execute(
            update(Notification)
            .where(Notification.read_at.is_(None), Notification.dismissed_at.is_(None))
            .values(read_at=datetime.now(UTC))
        )
        return {"updated": result.rowcount}


@router.delete("/notifications/{notification_id}")
def dismiss_notification(notification_id: int) -> dict:
    from datetime import UTC, datetime

    with session_scope() as session:
        row = session.get(Notification, notification_id)
        if row is None:
            raise HTTPException(status_code=404, detail="notification not found")
        row.dismissed_at = datetime.now(UTC)
    return {"dismissed": True}


@router.post("/runs/{run_id}/retry-notification")
def retry_notification(run_id: int) -> dict:
    try:
        return deliver_local_notification(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _serialize_export(row: AutomationExport) -> dict:
    return {
        "id": row.id,
        "automation_run_id": row.automation_run_id,
        "file_id": row.file_id,
        "format": row.format,
        "status": row.status,
        "sha256": row.sha256,
        "size_bytes": row.size_bytes,
        "provenance": row.provenance or {},
        "error": row.error,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


@router.post("/exports/{export_id}/retry")
def retry_export(export_id: int) -> dict:
    with session_scope() as session:
        row = session.get(AutomationExport, export_id)
        if row is None:
            raise HTTPException(status_code=404, detail="automation export not found")
        if row.automation_run_id is None:
            raise HTTPException(status_code=409, detail="source automation run was deleted")
        run_id, fmt = row.automation_run_id, row.format
    return deliver_automation_export(run_id, fmt)


@router.get("/exports/{export_id}/download")
def download_export(export_id: int):
    with session_scope() as session:
        row = session.get(AutomationExport, export_id)
        if row is None or row.status != "completed" or not row.path:
            raise HTTPException(status_code=404, detail="completed automation export not found")
        path = Path(row.path).resolve()
        expected_sha256 = row.sha256
        fmt = row.format
    root = get_settings().poller.download_dir.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="automation export file not found")
    if not expected_sha256 or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise HTTPException(status_code=409, detail="automation export checksum mismatch; retry it")
    return FileResponse(
        path,
        media_type="text/plain" if fmt == "txt" else f"text/{fmt}",
        filename=f"transcript.{fmt}",
    )
