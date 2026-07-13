"""User-triggered metadata-first imports and on-demand Plaud audio fetches."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select, update

from .config import Settings, get_settings
from .db.models import FileStatus, ImportRun, PlaudFile
from .db.session import session_scope
from .plaud import make_plaud_client
from .poller.poll import _apply_dto, _download_one, refresh_cloud_artifacts_for

_start_lock = threading.Lock()


def import_run_to_dict(row: ImportRun) -> dict:
    return {
        "id": row.id,
        "source": row.source,
        "status": row.status,
        "total": row.total,
        "processed": row.processed,
        "new": row.new_count,
        "changed": row.changed_count,
        "transcripts": row.transcript_count,
        "summaries": row.summary_count,
        "failed": row.failed_count,
        "error": row.error,
    }


def recover_interrupted_imports() -> int:
    with session_scope() as session:
        count = session.execute(
            update(ImportRun)
            .where(ImportRun.status.in_(("queued", "running")))
            .values(
                status="failed",
                error="Import interrupted by application restart; start it again.",
                completed_at=datetime.now(UTC),
            )
        ).rowcount
    return count


def latest_import_run() -> dict | None:
    with session_scope() as session:
        row = session.scalar(select(ImportRun).order_by(ImportRun.created_at.desc()))
        return import_run_to_dict(row) if row is not None else None


def start_plaud_metadata_import(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    with _start_lock, session_scope() as session:
        running = session.scalar(
            select(ImportRun).where(ImportRun.status.in_(("queued", "running")))
        )
        if running is not None:
            raise RuntimeError("a Plaud metadata import is already running")
        row = ImportRun(id=str(uuid4()), source="plaud", status="queued")
        session.add(row)
        session.flush()
        run_id = row.id
        result = import_run_to_dict(row)
    threading.Thread(
        target=_run_plaud_metadata_import,
        args=(run_id, settings),
        daemon=True,
        name=f"plaud-metadata-{run_id[:8]}",
    ).start()
    return result


def _run_plaud_metadata_import(run_id: str, settings: Settings) -> None:
    try:
        _update_run(run_id, status="running", started_at=datetime.now(UTC))
        with make_plaud_client(settings.plaud) as client:
            files = list(client.iter_files(include_trash=settings.poller.include_trash))
            _update_run(run_id, total=len(files))
            for dto in files:
                is_new = False
                is_changed = False
                with session_scope() as session:
                    row = session.get(PlaudFile, dto.id)
                    if row is None:
                        row = PlaudFile(
                            id=dto.id,
                            status=FileStatus.metadata_only,
                            origin="plaud",
                        )
                        session.add(row)
                        is_new = True
                    previous = row.raw or {}
                    incoming = dto.model_dump(exclude_unset=True)
                    is_changed = not is_new and any(
                        previous.get(key) != value for key, value in incoming.items()
                    )
                    _apply_dto(row, dto)
                    row.origin = "plaud"
                    if not row.audio_path and row.status != FileStatus.downloading:
                        row.status = FileStatus.metadata_only

                has_transcript = has_summary = False
                failed = False
                last_error = None
                try:
                    has_transcript, has_summary = refresh_cloud_artifacts_for(
                        client, dto.id
                    )
                except Exception as exc:  # noqa: BLE001 - continue the catalog import
                    failed = True
                    last_error = f"{dto.id}: {exc}"[:2000]
                try:
                    from .automations import evaluate_recording

                    evaluate_recording(dto.id)
                except Exception:  # noqa: BLE001 - importing must survive rule failures
                    pass
                _advance_run(
                    run_id,
                    is_new=is_new,
                    is_changed=is_changed,
                    has_transcript=has_transcript,
                    has_summary=has_summary,
                    failed=failed,
                    last_error=last_error,
                )
        _update_run(run_id, status="completed", completed_at=datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001
        _update_run(
            run_id,
            status="failed",
            error=str(exc)[:2000],
            completed_at=datetime.now(UTC),
        )


def _update_run(run_id: str, **values) -> None:
    values["updated_at"] = datetime.now(UTC)
    with session_scope() as session:
        session.execute(update(ImportRun).where(ImportRun.id == run_id).values(**values))


def _advance_run(
    run_id: str,
    *,
    is_new: bool,
    is_changed: bool,
    has_transcript: bool,
    has_summary: bool,
    failed: bool,
    last_error: str | None,
) -> None:
    with session_scope() as session:
        row = session.get(ImportRun, run_id)
        if row is None:
            return
        row.processed += 1
        row.new_count += int(is_new)
        row.changed_count += int(is_changed)
        row.transcript_count += int(has_transcript)
        row.summary_count += int(has_summary)
        row.failed_count += int(failed)
        if last_error:
            row.error = last_error


def start_plaud_audio_import(file_id: str, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise LookupError("recording not found")
        if row.audio_path:
            return {"file_id": file_id, "status": row.status.value, "has_audio": True}
        if row.status == FileStatus.downloading:
            return {"file_id": file_id, "status": "downloading", "has_audio": False}
        if row.origin != "plaud":
            raise ValueError("this recording is not backed by Plaud cloud audio")
        row.status = FileStatus.downloading
        row.error = None
        raw = dict(row.raw or {})
    threading.Thread(
        target=_run_audio_import,
        args=(file_id, raw, settings),
        daemon=True,
        name=f"plaud-audio-{file_id[:8]}",
    ).start()
    return {"file_id": file_id, "status": "downloading", "has_audio": False}


def _run_audio_import(file_id: str, raw: dict, settings: Settings) -> None:
    with make_plaud_client(settings.plaud) as client:
        _download_one(client, file_id, raw, settings, claim_acquired=True)


def audio_import_status(file_id: str) -> dict:
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise LookupError("recording not found")
        return {
            "file_id": file_id,
            "status": row.status.value,
            "has_audio": bool(row.audio_path),
            "error": row.error,
        }
