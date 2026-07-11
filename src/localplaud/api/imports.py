"""Metadata-first Plaud import and local audio upload API."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..db.models import FileStatus, PlaudFile
from ..db.session import session_scope
from ..imports import (
    audio_import_status,
    latest_import_run,
    start_plaud_audio_import,
    start_plaud_metadata_import,
)
from ..store.files import file_dir

router = APIRouter(prefix="/api/imports", tags=["imports"])
_ALLOWED_EXTENSIONS = {
    "mp3",
    "mp4",
    "m4a",
    "wav",
    "ogg",
    "opus",
    "webm",
    "flac",
    "aac",
}
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


@router.post("/plaud/metadata", status_code=202)
def import_plaud_metadata() -> dict:
    try:
        return start_plaud_metadata_import()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/plaud/metadata/status")
def import_plaud_metadata_status() -> dict:
    return latest_import_run() or {"status": "idle"}


@router.post("/plaud/files/{file_id}/audio", status_code=202)
def import_plaud_audio(file_id: str) -> dict:
    try:
        return start_plaud_audio_import(file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/plaud/files/{file_id}/audio/status")
def import_plaud_audio_status(file_id: str) -> dict:
    try:
        return audio_import_status(file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/local/audio", status_code=201)
def import_local_audio(file: Annotated[UploadFile, File()]) -> dict:
    original_name = Path(file.filename or "audio").name
    extension = Path(original_name).suffix.lower().lstrip(".")
    if extension not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="unsupported audio/video format",
        )
    file_id = f"local-{uuid4().hex}"
    destination = file_dir(file_id) / f"audio.{extension}"
    written = 0
    try:
        with destination.open("wb") as output:
            while chunk := file.file.read(1 << 20):
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="file exceeds 2 GB limit")
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    duration_ms = _probe_duration_ms(destination)
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add(
            PlaudFile(
                id=file_id,
                filename=Path(original_name).stem or "Imported audio",
                fullname=original_name,
                filesize=written,
                duration_ms=duration_ms,
                start_time_ms=int(now.timestamp() * 1000),
                status=FileStatus.downloaded,
                audio_path=str(destination),
                downloaded_at=now,
                origin="local",
                raw={"origin": "local", "original_filename": original_name},
            )
        )
    return {
        "id": file_id,
        "filename": Path(original_name).stem or "Imported audio",
        "filesize": written,
        "duration_ms": duration_ms,
        "status": "downloaded",
    }


def _probe_duration_ms(path: Path) -> int | None:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        )
        return int(float(result.stdout.strip()) * 1000)
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        return None
