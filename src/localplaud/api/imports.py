"""Metadata-first Plaud import and local audio upload API."""

from __future__ import annotations

import json
import math
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from ..db.models import FileStatus, PlaudFile
from ..db.session import session_scope
from ..imports import (
    audio_import_status,
    check_plaud_now,
    latest_import_run,
    start_plaud_audio_import,
    start_plaud_metadata_import,
)
from ..store.files import file_dir
from .upload_limits import MAX_UPLOAD_BYTES, UploadCleanupRoute

router = APIRouter(prefix="/api/imports", tags=["imports"])
_upload_router = APIRouter(route_class=UploadCleanupRoute)
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
    # Feature-phone / voice-memo recordings; ffmpeg decodes AMR-NB and AMR-WB
    # natively, so the convert stage handles them like any other input.
    "amr",
}
_MAX_UPLOAD_BYTES = MAX_UPLOAD_BYTES


@router.post("/plaud/check")
def check_plaud(request: Request) -> dict:
    """Check Plaud for new uploads now instead of waiting for the next poll."""
    from ..poller.poll import CatalogSyncBusy

    try:
        result = check_plaud_now()
    except CatalogSyncBusy:
        result = {"new": 0, "queued": 0, "recordings": [], "busy": True}
    except Exception as exc:  # noqa: BLE001 - provider adapters share no base error
        raise HTTPException(status_code=502, detail="Could not reach Plaud") from exc
    jobs = getattr(request.app.state, "daemon_jobs", None)
    if jobs is not None and (result["queued"] or result.get("busy")):
        # Download now; the next scheduled poll could be minutes away.
        jobs.request_sync()
    return result


@router.post("/plaud/metadata", status_code=202)
def import_plaud_metadata() -> dict:
    try:
        return start_plaud_metadata_import(refresh_artifacts=True)
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


@_upload_router.post("/local/audio", status_code=201)
def import_local_audio(file: Annotated[UploadFile, File()]) -> dict:
    original_name = Path(file.filename or "audio").name
    extension = Path(original_name).suffix.lower().lstrip(".")
    if extension not in _ALLOWED_EXTENSIONS:
        file.file.close()
        raise HTTPException(
            status_code=415,
            detail="unsupported audio/video format",
        )
    file_id = f"local-{uuid4().hex}"
    destination = None
    written = 0
    try:
        destination = file_dir(file_id) / f"audio.{extension}"
        with destination.open("wb") as output:
            while chunk := file.file.read(1 << 20):
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="file exceeds 2 GB limit")
                output.write(chunk)
        if not written:
            raise HTTPException(status_code=422, detail="audio file is empty")
        probe = _probe_audio(destination)
        now = datetime.now(UTC)
        with session_scope() as session:
            session.add(
                PlaudFile(
                    id=file_id,
                    filename=Path(original_name).stem or "Imported audio",
                    fullname=original_name,
                    filesize=written,
                    duration_ms=probe.duration_ms,
                    start_time_ms=int(now.timestamp() * 1000),
                    status=FileStatus.downloaded,
                    audio_path=str(destination),
                    downloaded_at=now,
                    origin="local",
                    raw={
                        "origin": "local",
                        "original_filename": original_name,
                        "media_probe": probe.status,
                    },
                )
            )
    except BaseException:
        if destination is not None:
            with suppress(OSError):
                destination.unlink(missing_ok=True)
                destination.parent.rmdir()
        raise
    finally:
        file.file.close()
    return {
        "id": file_id,
        "filename": Path(original_name).stem or "Imported audio",
        "filesize": written,
        "duration_ms": probe.duration_ms,
        "status": "downloaded",
        "media_probe": probe.status,
    }


router.include_router(_upload_router)


@dataclass(frozen=True)
class _AudioProbe:
    duration_ms: int | None
    status: str


def _probe_audio(path: Path) -> _AudioProbe:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=30,
        )
    except OSError:
        # Missing/unexecutable ffprobe is an environment limitation, not evidence
        # of corrupt input. Keep formats usable on installations without the tool.
        return _AudioProbe(None, "unavailable")
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=503, detail="audio probe timed out; try again") from exc
    except subprocess.CalledProcessError as exc:
        # The converter supports one verified headerless Plaud Opus layout that
        # ffprobe cannot identify. Preserve that existing format explicitly.
        if path.suffix.lower() == ".opus":
            from ..worker.convert import ConversionError, _raw_packet_count

            try:
                return _AudioProbe(_raw_packet_count(path) * 20, "verified_raw_opus")
            except ConversionError:
                pass
        error = (exc.stderr or "").lower()
        if any(
            marker in error
            for marker in (
                "decoder not found",
                "unknown decoder",
                "unsupported codec",
                "unrecognized option",
            )
        ):
            return _AudioProbe(None, "unavailable")
        raise HTTPException(
            status_code=422, detail="file could not be read as supported audio/video"
        ) from exc
    try:
        metadata = json.loads(result.stdout)
        streams = metadata["streams"]
        if not isinstance(streams, list) or any(not isinstance(stream, dict) for stream in streams):
            return _AudioProbe(None, "inconclusive")
        if not any(stream.get("codec_type") == "audio" for stream in streams):
            raise HTTPException(status_code=422, detail="file contains no audio stream")
        duration = metadata.get("format", {}).get("duration")
        try:
            seconds = float(duration)
        except (TypeError, ValueError):
            seconds = math.nan
        duration_ms = int(seconds * 1000) if math.isfinite(seconds) and seconds >= 0 else None
        return _AudioProbe(duration_ms, "verified")
    except (ValueError, KeyError, TypeError, AttributeError):
        return _AudioProbe(None, "inconclusive")
