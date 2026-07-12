"""Private workspace backup API."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..backups import (
    create_workspace_backup,
    delete_workspace_backup,
    list_workspace_backups,
    workspace_backup_path,
)

router = APIRouter(prefix="/api/backups", tags=["backups"])


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
