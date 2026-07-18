"""The poller — mirror the Plaud cloud into the local store.

Two steps, both read-only against the cloud:

1. ``sync_file_list`` — pull the file listing and upsert rows, marking new or
   changed files (by ``version``/``version_ms``) for (re)processing.
2. ``download_pending`` — download audio for files that need it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

from ..config import Settings, get_settings
from ..db.models import (
    FileStatus,
    KeyValue,
    PlaudFile,
    StageAttempt,
    StageName,
    StageRun,
    StageStatus,
)
from ..db.session import session_scope
from ..plaud import make_plaud_client
from ..plaud.common import _assert_safe_fetch_url
from ..plaud.models import PlaudFileDTO
from ..store.files import _safe_id, file_dir

log = logging.getLogger(__name__)

# Stash key inside PlaudFile.raw recording the filename at the last cloud-
# artifact check, so a rename (Plaud retitles a file when it summarizes it)
# triggers exactly one re-check instead of one per poll cycle.
_ARTIFACT_CHECKED_KEY = "_artifact_checked_name"
_CATALOG_BASELINE_KEY = "plaud_catalog_baseline_v1"
_CATALOG_SYNC_LOCK_KEY = "plaud_catalog_sync_lock_v1"
_CATALOG_SYNC_LOCK_TTL = timedelta(minutes=15)
_DAEMON_OWNER_KEY = "localplaud_daemon_owner_v1"
_DAEMON_HEARTBEAT_TTL = timedelta(minutes=5)
_DAEMON_HEARTBEAT_INTERVAL_SECONDS = 30
_DOWNLOAD_RECOVERY_TTL = timedelta(hours=1)
_DOWNLOAD_LEASE = timedelta(hours=1)
_ACTIVE_DAEMON_OWNER: str | None = None
_MAX_NOTE_ASSET_BYTES = 15 * 1024 * 1024
_NOTE_ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_MARKDOWN_IMAGE = re.compile(
    r"!\[([^\]\n]*)\]\(\s*(?:<([^>\n]+)>|([^\s)\n]+))\s*\)"
)


def _note_asset_extension(path: str, url: str) -> str | None:
    for value in (path, url):
        suffix = Path(urlsplit(value).path).suffix.lower()
        if suffix in _NOTE_ASSET_EXTENSIONS:
            return suffix
    return None


def _fetch_note_asset(url: str, *, path: str, destination: Path) -> str:
    _assert_safe_fetch_url(url)
    extension = _note_asset_extension(path, url)
    if extension is None:
        raise ValueError("unsupported note asset image type")
    content = bytearray()
    with httpx.Client(timeout=120, follow_redirects=False) as client, client.stream(
        "GET", url
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(chunk_size=1 << 16):
            content.extend(chunk)
            if len(content) > _MAX_NOTE_ASSET_BYTES:
                raise ValueError("note asset exceeds 15 MB")
    name = f"{hashlib.sha256(content).hexdigest()[:16]}{extension}"
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / name
    if not output.exists():
        temporary = destination / f".{name}.{uuid4().hex}.tmp"
        try:
            temporary.write_bytes(content)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    return name


def _mirror_note_assets(note: dict, *, file_id: str, settings: Settings) -> str:
    markdown = str(note["markdown"])
    assets = note.get("assets")
    if not isinstance(assets, dict) or not assets:
        return markdown
    destination = (
        Path(settings.poller.download_dir) / _safe_id(file_id) / "note-assets"
    )
    mirrored: dict[str, str | None] = {}

    def replace(match: re.Match) -> str:
        alt = match.group(1)
        path = match.group(2) or match.group(3)
        key = path if path in assets else unquote(path)
        url = assets.get(key)
        if not isinstance(url, str) or not url:
            return match.group(0)
        if key not in mirrored:
            try:
                mirrored[key] = _fetch_note_asset(url, path=path, destination=destination)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "note asset import failed for %s (%s): %s",
                    file_id,
                    path,
                    type(exc).__name__,
                )
                mirrored[key] = None
        name = mirrored[key]
        if name is None:
            return alt
        return f"![{alt}](/api/files/{file_id}/note-assets/{name})"

    return _MARKDOWN_IMAGE.sub(replace, markdown)


def _insert_key_if_absent(session, *, key: str, value: dict) -> bool:
    """Atomically insert a KeyValue row on SQLite, Postgres, or a generic DB."""
    dialect = session.get_bind().dialect.name
    values = {"key": key, "value": value}
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert

        result = session.execute(dialect_insert(KeyValue).values(**values).on_conflict_do_nothing())
        return result.rowcount == 1
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert

        result = session.execute(dialect_insert(KeyValue).values(**values).on_conflict_do_nothing())
        return result.rowcount == 1
    try:
        with session.begin_nested():
            session.execute(insert(KeyValue).values(**values))
        return True
    except IntegrityError:
        return False


def _claim_catalog_sync() -> str | None:
    """Return a durable sync token, or None when another poll owns the listing."""
    now = datetime.now(UTC)
    token = uuid4().hex
    with session_scope() as session:
        current = session.get(KeyValue, _CATALOG_SYNC_LOCK_KEY)
        if current is not None:
            claimed_at_raw = (current.value or {}).get("claimed_at")
            try:
                claimed_at = datetime.fromisoformat(str(claimed_at_raw))
                if claimed_at.tzinfo is None:
                    claimed_at = claimed_at.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                claimed_at = now - _CATALOG_SYNC_LOCK_TTL - timedelta(seconds=1)
            if claimed_at > now - _CATALOG_SYNC_LOCK_TTL:
                return None
            session.execute(
                delete(KeyValue).where(
                    KeyValue.key == _CATALOG_SYNC_LOCK_KEY,
                    KeyValue.updated_at <= now - _CATALOG_SYNC_LOCK_TTL,
                ),
                execution_options={"synchronize_session": False},
            )
            session.flush()
        claimed = _insert_key_if_absent(
            session,
            key=_CATALOG_SYNC_LOCK_KEY,
            value={"token": token, "claimed_at": now.isoformat()},
        )
    return token if claimed else None


def _release_catalog_sync(token: str) -> None:
    with session_scope() as session:
        row = session.get(KeyValue, _CATALOG_SYNC_LOCK_KEY)
        if row is not None and (row.value or {}).get("token") == token:
            session.delete(row)


def _catalog_baseline_complete() -> bool:
    with session_scope() as session:
        return session.get(KeyValue, _CATALOG_BASELINE_KEY) is not None


def _apply_dto(row: PlaudFile, dto: PlaudFileDTO) -> None:
    """Copy DTO fields onto the row. Only fields the provider actually sent
    are applied (official transports may omit version/md5/trash)."""
    provided = dto.model_fields_set
    row.filename = dto.filename or row.filename
    if "duration" in provided:
        row.duration_ms = dto.duration
    if "start_time" in provided:
        row.start_time_ms = dto.start_time
    if "end_time" in provided:
        row.end_time_ms = dto.end_time
    row.raw = {**(row.raw or {}), **dto.model_dump(exclude_unset=True)}


def sync_file_list(client, settings: Settings) -> tuple[int, int]:
    """Upsert the cloud listing. Returns (new_count, changed_count).

    Works with either official provider; both use the same file ids. A fresh
    workspace's first successful listing establishes a durable metadata-only
    baseline so enabling automatic download never backfills an entire Plaud
    history. Only recordings first observed after that baseline are queued for
    automatic raw-audio download.
    """
    sync_token = _claim_catalog_sync()
    if sync_token is None:
        log.info("Skipping Plaud listing because another poll owns the catalog sync")
        return (0, 0)
    try:
        new_count = changed_count = 0
        with session_scope() as session:
            catalog_initialized = session.get(KeyValue, _CATALOG_BASELINE_KEY) is not None
            if not catalog_initialized:
                # Upgrade safely from metadata-first deployments: old audio-less
                # queues and download errors must not become a historical backfill.
                session.execute(
                    update(PlaudFile)
                    .where(
                        PlaudFile.origin == "plaud",
                        PlaudFile.audio_path.is_(None),
                        PlaudFile.status.in_((FileStatus.discovered, FileStatus.error)),
                    )
                    .values(
                        status=FileStatus.metadata_only,
                        error=None,
                        download_token=None,
                        download_lease_until=None,
                    )
                )
            for dto in client.iter_files(include_trash=settings.poller.include_trash):
                row = session.get(PlaudFile, dto.id)
                if row is None:
                    row = PlaudFile(
                        id=dto.id,
                        status=(
                            FileStatus.discovered
                            if settings.poller.auto_download and catalog_initialized
                            else FileStatus.metadata_only
                        ),
                        origin="plaud",
                    )
                    _apply_dto(row, dto)
                    session.add(row)
                    new_count += 1
                    log.info("New file discovered: %s (%s)", dto.id, dto.filename)
                else:
                    _apply_dto(row, dto)
            if session.get(KeyValue, _CATALOG_BASELINE_KEY) is None:
                session.add(
                    KeyValue(
                        key=_CATALOG_BASELINE_KEY,
                        value={"completed_at": datetime.now(UTC).isoformat()},
                    )
                )
        return new_count, changed_count
    finally:
        _release_catalog_sync(sync_token)


def _processing_reset_statement(
    *, now: datetime, force: bool, previous_owner: str | None = None
):
    from ..worker.claims import daemon_token_pattern

    condition = PlaudFile.status == FileStatus.processing
    reclaimable = or_(
        PlaudFile.processing_lease_until.is_(None),
        PlaudFile.processing_lease_until <= now,
    )
    if force and previous_owner:
        reclaimable = or_(
            reclaimable,
            PlaudFile.processing_token.like(daemon_token_pattern(previous_owner)),
        )
    condition &= reclaimable
    return (
        update(PlaudFile)
        .where(condition)
        .values(
            status=FileStatus.downloaded,
            processing_token=None,
            processing_lease_until=None,
        )
        .returning(PlaudFile.id)
    )


def reset_inflight(*, force: bool = False, previous_owner: str | None = None) -> int:
    """Recover files stranded mid-flight by a crash/kill: ``downloading`` →
    ``discovered`` and expired ``processing`` → ``downloaded``. A daemon startup
    may force recovery of all in-flight rows from its previous process. Periodic
    polls preserve live leases owned by CLI or other workers. Any affected running
    stage and append-only attempt are closed as interrupted. Returns the number of
    file rows reset."""
    reset = 0
    now = datetime.now(UTC)
    interruption = "Interrupted by application restart; queued for retry."
    with session_scope() as session:
        from ..worker.claims import daemon_token_pattern

        download_reclaimable = or_(
            # New claims use an explicit lease. A missing lease is malformed and
            # cannot safely retain ownership indefinitely.
            (
                PlaudFile.download_token.is_not(None)
                & or_(
                    PlaudFile.download_lease_until.is_(None),
                    PlaudFile.download_lease_until <= now,
                )
            ),
            # Upgrade compatibility for a process that entered ``downloading``
            # before token columns existed.
            (
                PlaudFile.download_token.is_(None)
                & or_(
                    PlaudFile.download_lease_until <= now,
                    (
                        PlaudFile.download_lease_until.is_(None)
                        & or_(
                            PlaudFile.updated_at.is_(None),
                            PlaudFile.updated_at <= now - _DOWNLOAD_RECOVERY_TTL,
                        )
                    ),
                )
            ),
        )
        if force and previous_owner:
            download_reclaimable = or_(
                download_reclaimable,
                PlaudFile.download_token.like(daemon_token_pattern(previous_owner)),
            )
        reset += session.execute(
            update(PlaudFile)
            .where(
                PlaudFile.status == FileStatus.downloading,
                download_reclaimable,
            )
            .values(
                status=FileStatus.discovered,
                download_token=None,
                download_lease_until=None,
            )
        ).rowcount
        processing_ids = list(
            session.scalars(
                _processing_reset_statement(
                    now=now,
                    force=force,
                    previous_owner=previous_owner,
                )
            )
        )
        reset += len(processing_ids)
        # Reindex-only workers deliberately retain the recording's visible status,
        # so a non-processing row can still carry a live claim. Only clear claims
        # whose lease is absent or expired.
        non_processing_reclaimable = or_(
            PlaudFile.processing_lease_until.is_(None),
            PlaudFile.processing_lease_until <= now,
        )
        if force and previous_owner:
            non_processing_reclaimable = or_(
                non_processing_reclaimable,
                PlaudFile.processing_token.like(daemon_token_pattern(previous_owner)),
            )
        non_processing_ids = list(
            session.scalars(
                update(PlaudFile)
                .where(
                    PlaudFile.status != FileStatus.processing,
                    PlaudFile.processing_token.is_not(None),
                    non_processing_reclaimable,
                )
                .values(processing_token=None, processing_lease_until=None)
                .returning(PlaudFile.id)
            )
        )
        reset += len(non_processing_ids)
        interrupted_ids = [*processing_ids, *non_processing_ids]
        for run in session.scalars(
            select(StageRun).where(
                StageRun.file_id.in_(interrupted_ids),
                StageRun.status == StageStatus.running,
            )
        ):
            reindex_retry = (
                run.file_id in non_processing_ids
                and run.stage == StageName.index
                and bool((run.detail or {}).get("reindex_only"))
            )
            run.status = StageStatus.pending if reindex_retry else StageStatus.failed
            run.error = interruption
            run.completed_at = None if reindex_retry else now
            run.updated_at = now
        session.execute(
            update(StageAttempt)
            .where(
                StageAttempt.file_id.in_(interrupted_ids),
                StageAttempt.status == StageStatus.running,
            )
            .values(
                status=StageStatus.failed,
                error=interruption,
                completed_at=now,
            )
        )
    if reset:
        log.info("Reset %d in-flight file(s) after restart", reset)
    return reset


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_start_fingerprint(pid: int) -> str | None:
    """Hash the OS-reported process birth time without persisting command details."""
    if pid <= 0:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = " ".join(result.stdout.split())
    if result.returncode != 0 or not started:
        return None
    return hashlib.sha256(started.encode()).hexdigest()[:24]


def _daemon_record_is_live(value: dict, *, now: datetime, hostname: str) -> bool:
    if value.get("released_at"):
        return False
    existing_host = str(value.get("hostname") or "")
    try:
        existing_pid = int(value.get("pid") or 0)
    except (TypeError, ValueError):
        existing_pid = 0
    if existing_host == hostname and existing_pid:
        if not _pid_is_running(existing_pid):
            return False
        expected_start = value.get("process_start_fingerprint")
        if not expected_start:
            # Preserve a live pre-fingerprint daemon during a rolling upgrade.
            return True
        actual_start = _process_start_fingerprint(existing_pid)
        # Failure to inspect a running process is treated conservatively. A known
        # mismatch proves PID reuse and must not block a replacement daemon.
        return actual_start is None or actual_start == expected_start
    heartbeat_raw = value.get("heartbeat_at")
    try:
        heartbeat = datetime.fromisoformat(str(heartbeat_raw))
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return False
    return heartbeat > now - _DAEMON_HEARTBEAT_TTL


def register_daemon_owner() -> tuple[str, str | None]:
    """Register this process and return ``(owner, crashed_previous_owner)``."""
    global _ACTIVE_DAEMON_OWNER

    now = datetime.now(UTC)
    hostname = socket.gethostname()
    owner = uuid4().hex[:16]
    value = {
        "owner": owner,
        "hostname": hostname,
        "pid": os.getpid(),
        "process_start_fingerprint": _process_start_fingerprint(os.getpid()),
        "started_at": now.isoformat(),
        "heartbeat_at": now.isoformat(),
    }
    previous_owner = None
    with session_scope() as session:
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            session.execute(sql_text("BEGIN IMMEDIATE"))
        stmt = select(KeyValue).where(KeyValue.key == _DAEMON_OWNER_KEY)
        if dialect == "postgresql":
            stmt = stmt.with_for_update()
        row = session.scalar(stmt)
        if row is None:
            if _insert_key_if_absent(session, key=_DAEMON_OWNER_KEY, value=value):
                _ACTIVE_DAEMON_OWNER = owner
                return owner, None
            row = session.scalar(stmt)
        if row is None:
            raise RuntimeError("could not acquire the daemon ownership record")
        current = row.value or {}
        if _daemon_record_is_live(current, now=now, hostname=hostname):
            raise RuntimeError("another localplaud daemon is already running")
        previous_owner = current.get("owner") or None
        row.value = value
        row.updated_at = now
    _ACTIVE_DAEMON_OWNER = owner
    return owner, previous_owner


def refresh_daemon_owner(owner: str) -> bool:
    now = datetime.now(UTC)
    with session_scope() as session:
        current = session.scalar(
            select(KeyValue.value).where(KeyValue.key == _DAEMON_OWNER_KEY)
        )
        if current is None or (current or {}).get("owner") != owner:
            return False
        refreshed = session.execute(
            update(KeyValue)
            .where(
                KeyValue.key == _DAEMON_OWNER_KEY,
                KeyValue.value["owner"].as_string() == owner,
                KeyValue.value["released_at"].as_string().is_(None),
            )
            .values(
                value={**current, "heartbeat_at": now.isoformat()},
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
    return refreshed == 1


def current_daemon_owner() -> str | None:
    return _ACTIVE_DAEMON_OWNER


def release_daemon_owner(owner: str) -> bool:
    """Mark a graceful shutdown without erasing the recoverable owner epoch."""
    global _ACTIVE_DAEMON_OWNER

    now = datetime.now(UTC)
    with session_scope() as session:
        current = session.scalar(
            select(KeyValue.value).where(KeyValue.key == _DAEMON_OWNER_KEY)
        )
        if current is None or (current or {}).get("owner") != owner:
            if _ACTIVE_DAEMON_OWNER == owner:
                _ACTIVE_DAEMON_OWNER = None
            return False
        released = session.execute(
            update(KeyValue)
            .where(
                KeyValue.key == _DAEMON_OWNER_KEY,
                KeyValue.value["owner"].as_string() == owner,
            )
            .values(
                value={**current, "released_at": now.isoformat()},
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
    if _ACTIVE_DAEMON_OWNER == owner:
        _ACTIVE_DAEMON_OWNER = None
    return released == 1


def reset_download_errors() -> int:
    """Give failed downloads another chance each cycle: ``error`` rows that
    never got audio on disk go back to ``discovered``. Download failures are
    dominated by transient causes (rate limits, expired presigned URLs,
    network); pipeline errors keep their audio_path and are NOT retried here.
    Returns the number of rows reset."""
    from sqlalchemy import update

    with session_scope() as session:
        reset = session.execute(
            update(PlaudFile)
            .where(PlaudFile.status == FileStatus.error, PlaudFile.audio_path.is_(None))
            .values(
                status=FileStatus.discovered,
                error=None,
                download_token=None,
                download_lease_until=None,
            )
        ).rowcount
    if reset:
        log.info("Retrying %d failed download(s)", reset)
    return reset


def _download_one(
    client,
    file_id: str,
    raw: dict,
    settings: Settings,
    *,
    claim_acquired: bool = False,
) -> bool:
    from ..worker.claims import (
        current_processing_owner,
        new_processing_token,
        processing_owner,
    )

    dest_dir = file_dir(file_id)
    dto = PlaudFileDTO.model_validate(raw or {"id": file_id})
    claim_owner = current_processing_owner() or current_daemon_owner()
    with processing_owner(claim_owner):
        token = new_processing_token()
    claim_dir = dest_dir / ".download-claims" / hashlib.sha256(token.encode()).hexdigest()
    try:
        now = datetime.now(UTC)
        with session_scope() as session:
            claimable_status = (
                PlaudFile.status == FileStatus.downloading
                if claim_acquired
                else PlaudFile.status == FileStatus.discovered
            )
            claimed = session.execute(
                update(PlaudFile)
                .where(
                    PlaudFile.id == file_id,
                    claimable_status,
                    PlaudFile.download_token.is_(None),
                )
                .values(
                    status=FileStatus.downloading,
                    error=None,
                    download_token=token,
                    download_lease_until=now + _DOWNLOAD_LEASE,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
        if claimed != 1:
            return False
        claim_dir.mkdir(parents=True, exist_ok=True)
        dest = Path(client.download_audio(dto, claim_dir))
        if not dest.resolve().is_relative_to(claim_dir.resolve()):
            raise ValueError("download client returned a path outside its claim directory")
        with session_scope() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(sql_text("BEGIN IMMEDIATE"))
                fresh = session.get(PlaudFile, file_id)
            else:
                fresh = session.scalar(
                    select(PlaudFile).where(PlaudFile.id == file_id).with_for_update()
                )
            lease = fresh.download_lease_until if fresh is not None else None
            if lease is not None and lease.tzinfo is None:
                lease = lease.replace(tzinfo=UTC)
            if (
                fresh is None
                or fresh.download_token != token
                or lease is None
                or lease <= datetime.now(UTC)
            ):
                return False
            final_path = dest_dir / dest.name
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(dest, final_path)
            fresh.audio_path = str(final_path)
            fresh.status = FileStatus.downloaded
            fresh.downloaded_at = datetime.now(UTC)
            fresh.error = None
            fresh.download_token = None
            fresh.download_lease_until = None
        if settings.pipeline.cloud_import_enabled:
            # The official client already fetched (and cached) the detail
            # payload for the presigned URL. Explicit migration mode may retain
            # Plaud's transcript/summary for comparison or backfill.
            try:
                _ingest_artifacts_for(client, file_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("cloud artifact ingest failed for %s: %s", file_id, exc)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Download failed for %s: %s", file_id, exc)
        with session_scope() as session:
            session.execute(
                update(PlaudFile)
                .where(
                    PlaudFile.id == file_id,
                    PlaudFile.download_token == token,
                    PlaudFile.download_lease_until > datetime.now(UTC),
                )
                .values(
                    status=FileStatus.error,
                    error=str(exc)[:2000],
                    download_token=None,
                    download_lease_until=None,
                )
                .execution_options(synchronize_session=False)
            )
        return False
    finally:
        shutil.rmtree(claim_dir, ignore_errors=True)


def download_pending(client, settings: Settings) -> int:
    """Download audio for discovered files, up to
    ``poller.max_concurrent_downloads`` at a time. Returns count downloaded."""
    with session_scope() as session:
        stmt = select(PlaudFile.id, PlaudFile.raw).where(PlaudFile.status == FileStatus.discovered)
        if not settings.poller.include_trash:
            stmt = stmt.where(PlaudFile.is_trash.is_(False))
        pending = [(fid, raw) for fid, raw in session.execute(stmt)]
    if not pending:
        return 0

    from ..worker.claims import current_processing_owner, processing_owner

    workers = max(1, settings.poller.max_concurrent_downloads)
    claim_owner = current_processing_owner()

    def run_download(item: tuple[str, dict]) -> bool:
        with processing_owner(claim_owner):
            return _download_one(client, item[0], item[1], settings)

    if workers == 1:
        return sum(run_download(item) for item in pending)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(run_download, pending)
    return sum(results)


def _looks_like_raw_name(name: str) -> bool:
    """Plaud's default filename is the recording timestamp; it retitles the
    file when its cloud AI summarizes it. A non-timestamp name is therefore a
    strong signal that cloud artifacts exist."""
    import re

    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", (name or "").strip()))


def _ingest_artifacts_for(client, file_id: str) -> bool:
    """Mirror Plaud's own transcript + summary for one file (idempotent).
    Returns True if anything new was stored."""
    from ..db.models import Summary as SummaryRow
    from ..db.models import Transcript as TranscriptRow

    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            return False
        filename = row.filename
        has_summary = any(s.template == "plaud" for s in row.summaries)
        has_transcript = any(t.source in {"cloud", "plaud"} for t in row.transcripts)

    stored = False
    md = None if has_summary else client.get_cloud_summary_md(file_id)
    if md:
        title = next((ln[2:].strip() for ln in md.splitlines() if ln.startswith("# ")), None)
        with session_scope() as session:
            session.add(
                SummaryRow(
                    file_id=file_id,
                    template="plaud",
                    title=title,
                    content_md=md,
                    source="cloud",
                )
            )
            session.get(PlaudFile, file_id).cloud_is_summary = True
        stored = True

    segments = None if has_transcript else client.get_cloud_transcript_segments(file_id)
    if segments:
        with session_scope() as session:
            session.add(
                TranscriptRow(
                    file_id=file_id,
                    provider="plaud",
                    source="cloud",
                    has_speakers=any(s.get("speaker") for s in segments),
                    text="\n".join(s["text"] for s in segments if s.get("text")),
                    segments=segments,
                )
            )
            session.get(PlaudFile, file_id).cloud_is_trans = True
            # If the operator later returns to independent mode, the one-time
            # backlog preparation must run again for this newly imported row.
            from ..db.migrations import INDEPENDENT_MIGRATION_KEY
            from ..db.models import KeyValue

            marker = session.get(KeyValue, INDEPENDENT_MIGRATION_KEY)
            if marker is not None:
                session.delete(marker)
        stored = True

    # Remember which filename this check ran under, so we only re-check after
    # a rename instead of on every poll cycle.
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        row.raw = {**(row.raw or {}), _ARTIFACT_CHECKED_KEY: filename}
    return stored


def refresh_cloud_artifacts_for(client, file_id: str) -> tuple[bool, bool]:
    """Refresh Plaud transcript/summary for one metadata import.

    Returns ``(transcript_present, summary_present)``. Existing cloud rows are
    replaced only after a successful detail fetch, so a network failure never
    destroys the last mirrored artifact.
    """
    from ..db.models import Summary as SummaryRow
    from ..db.models import Transcript as TranscriptRow

    detail = client.get_detail(file_id)
    notes = client.get_cloud_notes(file_id, detail)
    segments = client.get_cloud_transcript_segments(file_id, detail)
    settings = get_settings()
    mirrored_notes = [
        {**note, "markdown": _mirror_note_assets(note, file_id=file_id, settings=settings)}
        for note in notes
    ]
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            return (False, False)
        session.execute(
            delete(SummaryRow).where(
                SummaryRow.file_id == file_id, SummaryRow.source.in_(("cloud", "plaud"))
            )
        )
        session.execute(
            delete(TranscriptRow).where(
                TranscriptRow.file_id == file_id,
                TranscriptRow.source.in_(("cloud", "plaud")),
            )
        )
        for note in mirrored_notes:
            session.add(
                SummaryRow(
                    file_id=file_id,
                    template=note["key"],
                    title=note["title"],
                    content_md=note["markdown"],
                    source="cloud",
                )
            )
        if segments:
            session.add(
                TranscriptRow(
                    file_id=file_id,
                    provider="plaud",
                    source="cloud",
                    has_speakers=any(segment.get("speaker") for segment in segments),
                    text="\n".join(segment["text"] for segment in segments if segment.get("text")),
                    segments=segments,
                )
            )
        row.cloud_is_summary = bool(mirrored_notes)
        row.cloud_is_trans = bool(segments)
        row.raw = {
            **(row.raw or {}),
            _ARTIFACT_CHECKED_KEY: row.filename,
        }
    return (bool(segments), bool(mirrored_notes))


def ingest_cloud_artifacts(client, settings: Settings) -> int:
    """Mirror Plaud's own transcript (with speakers) and summary (markdown)
    for files that have them, stored as ``source="cloud"`` rows. Automatic use is
    restricted to explicit migration mode; independent mode never treats these
    rows as canonical pipeline output.

    Candidates: files whose cloud flags say an artifact exists but isn't
    mirrored yet, plus files renamed since the last check (Plaud retitles a
    file when its cloud AI processes it). Returns count of files that gained
    at least one artifact."""
    with session_scope() as session:
        rows = session.scalars(select(PlaudFile)).all()
        candidates = []
        for r in rows:
            missing_summary = r.cloud_is_summary and not any(
                s.template == "plaud" for s in r.summaries
            )
            has_cloud_transcript = any(t.source in {"cloud", "plaud"} for t in r.transcripts)
            missing_transcript = r.cloud_is_trans and not has_cloud_transcript
            renamed = (
                bool(r.filename)
                and not _looks_like_raw_name(r.filename)
                and (r.raw or {}).get(_ARTIFACT_CHECKED_KEY) != r.filename
                and (
                    not any(s.template == "plaud" for s in r.summaries) or not has_cloud_transcript
                )
            )
            if missing_summary or missing_transcript or renamed:
                candidates.append(r.id)

    ingested = 0
    for fid in candidates:
        try:
            if _ingest_artifacts_for(client, fid):
                ingested += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("cloud artifact ingest failed for %s: %s", fid, exc)
    return ingested


def poll_once(settings: Settings | None = None) -> dict:
    """One full poll cycle: sync the official listing + download pending
    (+ mirror Plaud's own transcripts/summaries when
    explicit migration mode is enabled)."""
    settings = settings or get_settings()
    reset_inflight()
    baseline_complete = _catalog_baseline_complete()
    if settings.poller.auto_download and baseline_complete:
        reset_download_errors()
    with make_plaud_client(settings.plaud) as client:
        new, changed = sync_file_list(client, settings)
        downloaded = (
            download_pending(client, settings)
            if settings.poller.auto_download and baseline_complete
            else 0
        )
        cloud_artifacts = (
            ingest_cloud_artifacts(client, settings)
            if settings.pipeline.cloud_import_enabled
            else 0
        )
    result = {
        "new": new,
        "changed": changed,
        "downloaded": downloaded,
        "cloud_artifacts": cloud_artifacts,
    }
    try:
        from ..automations import evaluate_library

        result["automated"] = evaluate_library()
    except Exception as exc:  # noqa: BLE001
        log.warning("AutoFlow evaluation failed after poll: %s", exc)
        result["automated"] = 0
    log.info("Poll cycle complete: %s", result)
    return result
