"""Private workspace backup API."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ..backup_sync import (
    delete_destination,
    deliver_backup,
    list_deliveries,
    list_destinations,
    retry_delivery,
    save_destination,
    test_destination,
)
from ..backups import (
    create_workspace_backup,
    delete_workspace_backup,
    list_workspace_backups,
    workspace_backup_path,
)
from ..db.session import session_scope
from ..error_redaction import sanitize_error

router = APIRouter(prefix="/api/backups", tags=["backups"])


class BackupDestinationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2048)
    secret_ref: str | None = Field(default=None, max_length=256)
    enabled: bool = True
    allow_private_network: bool = False


@router.get("")
def list_backups() -> dict:
    try:
        return {"backups": list_workspace_backups()}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("", status_code=201)
def create_backup(include_media: bool = False) -> dict:
    try:
        return create_workspace_backup(include_media=include_media)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{name}/download")
def download_backup(name: str):
    try:
        path = workspace_backup_path(name)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="backup not found") from exc
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.delete("/{name}")
def remove_backup(name: str) -> dict:
    try:
        delete_workspace_backup(name)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="backup not found") from exc
    return {"deleted": True}


@router.get("/destinations/list")
def backup_destinations() -> dict:
    with session_scope() as session:
        return {"destinations": list_destinations(session)}


@router.post("/destinations", status_code=201)
def create_backup_destination(body: BackupDestinationBody) -> dict:
    with session_scope() as session:
        try:
            return save_destination(session, body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/destinations/{destination_id}")
def update_backup_destination(destination_id: int, body: BackupDestinationBody) -> dict:
    with session_scope() as session:
        try:
            return save_destination(session, body.model_dump(), destination_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/destinations/{destination_id}/test")
def test_backup_destination(destination_id: int) -> dict:
    with session_scope() as session:
        try:
            return test_destination(session, destination_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/destinations/{destination_id}", status_code=204)
def remove_backup_destination(destination_id: int):
    with session_scope() as session:
        try:
            delete_destination(session, destination_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/sync-deliveries")
def backup_sync_deliveries(limit: int = 100) -> dict:
    with session_scope() as session:
        return {"deliveries": list_deliveries(session, limit)}


@router.post("/{name}/sync/{destination_id}")
def sync_backup(name: str, destination_id: int) -> dict:
    try:
        return deliver_backup(name, destination_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="backup not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - durable failure returned to UI
        raise HTTPException(status_code=502, detail=sanitize_error(exc)) from exc


@router.post("/sync-deliveries/{delivery_id}/retry")
def retry_backup_sync(delivery_id: int) -> dict:
    try:
        return retry_delivery(delivery_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - durable failure returned to UI
        raise HTTPException(status_code=502, detail=sanitize_error(exc)) from exc
