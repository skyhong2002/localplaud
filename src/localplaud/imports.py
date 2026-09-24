"""User-triggered metadata-first imports and on-demand Plaud audio fetches."""

from __future__ import annotations

import hashlib
import math
import threading
import time
import wave
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, update

from .config import Settings, get_settings
from .db.models import FileStatus, ImportRun, PlaudFile, Summary, Transcript
from .db.session import session_scope
from .plaud import make_plaud_client
from .poller.poll import _apply_dto, _download_one, refresh_cloud_artifacts_for
from .worker.convert import ConversionError, _raw_packet_count, to_wav

_start_lock = threading.Lock()
_playback_locks_lock = threading.Lock()
_playback_locks: dict[str, threading.Lock] = {}
_PLAUD_FULL_SYNC_DELAY_SECONDS = 1.0
_PLAUD_RATE_LIMIT_RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0)


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
        "skipped": row.skipped_count,
        "error": row.error,
    }


def recover_interrupted_imports() -> int:
    with session_scope() as session:
        count = session.execute(
            update(ImportRun)
            .where(ImportRun.status.in_(("queued", "running")))
            .values(
                status="failed",
                error=(
                    "Import interrupted by application restart; start it again; "
                    "already-synced recordings will be skipped."
                ),
                completed_at=datetime.now(UTC),
            )
        ).rowcount
    return count


def latest_import_run() -> dict | None:
    with session_scope() as session:
        row = session.scalar(select(ImportRun).order_by(ImportRun.created_at.desc()))
        return import_run_to_dict(row) if row is not None else None


def start_plaud_metadata_import(
    settings: Settings | None = None, *, refresh_artifacts: bool = False
) -> dict:
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
        args=(run_id, settings, refresh_artifacts),
        daemon=True,
        name=f"plaud-metadata-{run_id[:8]}",
    ).start()
    return result


def _is_plaud_rate_limit_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in ("429", "rate limit", "too many requests"))


def _refresh_cloud_artifacts_with_retry(client, file_id: str) -> tuple[bool, bool]:
    """Retry only transient Plaud throttling; preserve all other failures."""
    for delay in (*_PLAUD_RATE_LIMIT_RETRY_DELAYS, None):
        try:
            return refresh_cloud_artifacts_for(client, file_id)
        except Exception as exc:  # noqa: BLE001 - provider adapters share no base error
            if delay is None or not _is_plaud_rate_limit_error(exc):
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def _run_plaud_metadata_import(
    run_id: str, settings: Settings, refresh_artifacts: bool = False
) -> None:
    try:
        _update_run(run_id, status="running", started_at=datetime.now(UTC))
        with make_plaud_client(settings.plaud) as client:
            files = list(client.iter_files(include_trash=settings.poller.include_trash))
            _update_run(run_id, total=len(files))
            for dto in files:
                is_new = False
                is_changed = False
                needs_refresh = False
                has_transcript = has_summary = False
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
                    needs_refresh = refresh_artifacts or (
                        is_new or is_changed or row.cloud_artifacts_synced_at is None
                    )
                    _apply_dto(row, dto)
                    row.origin = "plaud"
                    if not row.audio_path and row.status != FileStatus.downloading:
                        row.status = FileStatus.metadata_only
                    if needs_refresh:
                        row.cloud_artifacts_synced_at = None
                    else:
                        cloud_sources = ("cloud", "plaud")
                        has_transcript = (
                            session.scalar(
                                select(Transcript.id)
                                .where(
                                    Transcript.file_id == dto.id,
                                    Transcript.source.in_(cloud_sources),
                                )
                                .limit(1)
                            )
                            is not None
                        )
                        has_summary = (
                            session.scalar(
                                select(Summary.id)
                                .where(
                                    Summary.file_id == dto.id,
                                    Summary.source.in_(cloud_sources),
                                )
                                .limit(1)
                            )
                            is not None
                        )

                failed = False
                last_error = None
                if needs_refresh:
                    try:
                        has_transcript, has_summary = _refresh_cloud_artifacts_with_retry(
                            client, dto.id
                        )
                        with session_scope() as session:
                            row = session.get(PlaudFile, dto.id)
                            if row is not None:
                                row.cloud_artifacts_synced_at = datetime.now(UTC)
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
                    skipped=not needs_refresh,
                    last_error=last_error,
                )
                if refresh_artifacts and needs_refresh:
                    # The official MCP throttles bursty full-library detail calls.
                    # A small steady delay is much faster than repeatedly failing
                    # most of a long import and restarting it from the beginning.
                    time.sleep(_PLAUD_FULL_SYNC_DELAY_SECONDS)
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
    skipped: bool,
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
        row.skipped_count += int(skipped)
        if last_error:
            row.error = last_error


def start_plaud_audio_import(file_id: str, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise LookupError("recording not found")
        path = Path(row.audio_path) if row.audio_path else None
        if path is not None and path.is_file():
            return {"file_id": file_id, "status": row.status.value, "has_audio": True}
        if row.audio_path:
            row.audio_path = None
            row.wav_path = None
        lease = row.download_lease_until
        if lease is not None and lease.tzinfo is None:
            lease = lease.replace(tzinfo=UTC)
        if row.status == FileStatus.downloading or (
            row.download_token and lease is not None and lease > datetime.now(UTC)
        ):
            return {"file_id": file_id, "status": "downloading", "has_audio": False}
        if (row.origin or "plaud") != "plaud":
            raise ValueError("this recording is not backed by Plaud cloud audio")
        cache_only = row.status == FileStatus.done
        if not cache_only:
            row.status = FileStatus.downloading
            row.error = None
        raw = dict(row.raw or {})
    threading.Thread(
        target=_run_audio_import,
        args=(file_id, raw, settings, cache_only),
        daemon=True,
        name=f"plaud-audio-{file_id[:8]}",
    ).start()
    return {
        "file_id": file_id,
        "status": "downloading" if not cache_only else "done",
        "has_audio": False,
    }


def _run_audio_import(
    file_id: str, raw: dict, settings: Settings, cache_only: bool = False
) -> None:
    with make_plaud_client(settings.plaud) as client:
        _download_one(
            client,
            file_id,
            raw,
            settings,
            claim_acquired=not cache_only,
            cache_only=cache_only,
        )


def ensure_plaud_audio(
    file_id: str,
    settings: Settings | None = None,
    *,
    timeout_seconds: float = 180.0,
) -> Path:
    """Return local audio, restoring a completed Plaud cache on demand.

    The download claim is durable and shared with imports/polling. Concurrent
    playback, waveform, export, or reprocess requests therefore wait for one
    publisher instead of downloading the same recording multiple times.
    """
    settings = settings or get_settings()
    deadline = time.monotonic() + timeout_seconds
    attempted = False
    while True:
        with session_scope() as session:
            row = session.get(PlaudFile, file_id)
            if row is None:
                raise LookupError("recording not found")
            path = Path(row.audio_path) if row.audio_path else None
            if path is not None and path.is_file():
                return path
            if (row.origin or "plaud") != "plaud":
                raise ValueError("recording audio is unavailable and is not backed by Plaud")
            if row.status != FileStatus.done:
                raise ValueError("recording audio has not been imported")
            if row.audio_path and path is not None and not path.exists():
                row.audio_path = None
                row.wav_path = None
            raw = dict(row.raw or {})
            lease = row.download_lease_until
            if lease is not None and lease.tzinfo is None:
                lease = lease.replace(tzinfo=UTC)
            download_active = bool(
                row.download_token and lease is not None and lease > datetime.now(UTC)
            )

        if not download_active and not attempted:
            attempted = True
            with make_plaud_client(settings.plaud) as client:
                if _download_one(
                    client,
                    file_id,
                    raw,
                    settings,
                    cache_only=True,
                    raise_errors=True,
                ):
                    continue
        elif attempted and not download_active:
            raise RuntimeError("Plaud audio download did not publish a local cache")
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for Plaud audio download")
        time.sleep(0.25)


def _playback_lock(source: Path) -> threading.Lock:
    key = str(source.resolve())
    with _playback_locks_lock:
        return _playback_locks.setdefault(key, threading.Lock())


def _valid_playback_wav(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as audio:
            valid_format = (
                audio.getnchannels() == 1
                and audio.getsampwidth() == 2
                and audio.getframerate() == 16000
                and audio.getnframes() > 0
            )
            if not valid_format or len(audio.readframes(1)) != 2:
                return False
            audio.setpos(audio.getnframes() - 1)
            return len(audio.readframes(1)) == 2
    except (FileNotFoundError, OSError, EOFError, wave.Error):
        return False


def ensure_playable_audio(
    file_id: str,
    settings: Settings | None = None,
    *,
    timeout_seconds: float = 180.0,
) -> Path:
    """Return browser-playable audio without changing the original-audio cache."""
    source = ensure_plaud_audio(file_id, settings, timeout_seconds=timeout_seconds)
    if source.suffix.lower() != ".opus":
        return source
    with source.open("rb") as stream:
        if stream.read(4) == b"OggS":
            return source

    # The database is read and closed before any validation or conversion work.
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise LookupError("recording not found")
        if not row.audio_path or Path(row.audio_path) != source:
            raise ConversionError("recording audio source changed during playback preparation")
        duration_ms = row.duration_ms
    if duration_ms is None or duration_ms <= 0:
        raise ConversionError("headerless Opus requires recording duration metadata")
    expected_seconds = duration_ms / 1000

    with _playback_lock(source):
        before = source.stat()
        packet_count = _raw_packet_count(source)
        actual_seconds = packet_count * 0.02
        if not math.isfinite(expected_seconds) or abs(actual_seconds - expected_seconds) > max(
            1.0, 0.02 * expected_seconds
        ):
            raise ConversionError("headerless Opus duration differs from recording metadata")
        identity = (
            str(source.resolve()),
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        digest = hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()[:24]
        cache_dir = source.parent / f".localplaud-playback-{digest}"
        playback = cache_dir / "audio.wav"
        if not _valid_playback_wav(playback):
            cache_dir.mkdir(parents=True, exist_ok=True)
            to_wav(source, playback, expected_duration_seconds=expected_seconds)
            if not _valid_playback_wav(playback):
                playback.unlink(missing_ok=True)
                raise ConversionError("converted playback audio is invalid")
        after = source.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ConversionError("recording audio source changed during playback preparation")
        return playback


def audio_import_status(file_id: str) -> dict:
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise LookupError("recording not found")
        return {
            "file_id": file_id,
            "status": "downloading" if row.download_token else row.status.value,
            "has_audio": bool(row.audio_path and Path(row.audio_path).is_file()),
            "error": row.error,
        }
