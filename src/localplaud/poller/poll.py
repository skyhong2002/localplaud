"""The poller — mirror the Plaud cloud into the local store.

Two steps, both read-only against the cloud:

1. ``sync_file_list`` — pull the file listing and upsert rows, marking new or
   changed files (by ``version``/``version_ms``) for (re)processing.
2. ``download_pending`` — download audio for files that need it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from ..config import Settings, get_settings
from ..db.models import FileStatus, KeyValue, PlaudFile, StageAttempt, StageRun, StageStatus
from ..db.session import session_scope
from ..plaud import make_plaud_client
from ..plaud.models import PlaudFileDTO
from ..store.files import file_dir

log = logging.getLogger(__name__)

# Stash key inside PlaudFile.raw recording the filename at the last cloud-
# artifact check, so a rename (Plaud retitles a file when it summarizes it)
# triggers exactly one re-check instead of one per poll cycle.
_ARTIFACT_CHECKED_KEY = "_artifact_checked_name"
_CATALOG_BASELINE_KEY = "plaud_catalog_baseline_v1"
_CATALOG_SYNC_LOCK_KEY = "plaud_catalog_sync_lock_v1"
_CATALOG_SYNC_LOCK_TTL = timedelta(minutes=15)


def _insert_key_if_absent(session, *, key: str, value: dict) -> bool:
    """Atomically insert a KeyValue row on SQLite, Postgres, or a generic DB."""
    dialect = session.get_bind().dialect.name
    values = {"key": key, "value": value}
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert

        result = session.execute(
            dialect_insert(KeyValue).values(**values).on_conflict_do_nothing()
        )
        return result.rowcount == 1
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert

        result = session.execute(
            dialect_insert(KeyValue).values(**values).on_conflict_do_nothing()
        )
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


def reset_inflight(*, force: bool = False) -> int:
    """Recover files stranded mid-flight by a crash/kill: ``downloading`` →
    ``discovered`` and expired ``processing`` → ``downloaded``. A daemon startup
    may force recovery of all in-flight rows from its previous process. Periodic
    polls preserve live leases owned by CLI or other workers. Any affected running
    stage and append-only attempt are closed as interrupted. Returns the number of
    file rows reset."""
    from sqlalchemy import update

    reset = 0
    now = datetime.now(UTC)
    interruption = "Interrupted by application restart; queued for retry."
    with session_scope() as session:
        if force:
            reset += session.execute(
                update(PlaudFile)
                .where(PlaudFile.status == FileStatus.downloading)
                .values(status=FileStatus.discovered)
            ).rowcount
        processing_condition = PlaudFile.status == FileStatus.processing
        if not force:
            processing_condition &= or_(
                PlaudFile.processing_lease_until.is_(None),
                PlaudFile.processing_lease_until <= now,
            )
        processing_ids = list(
            session.scalars(select(PlaudFile.id).where(processing_condition))
        )
        reset += session.execute(
            update(PlaudFile)
            .where(PlaudFile.id.in_(processing_ids))
            .values(
                status=FileStatus.downloaded,
                processing_token=None,
                processing_lease_until=None,
            )
        ).rowcount
        # Older workers could persist error/partial before their claim-finally
        # path ran, leaving a future lease that made an otherwise due retry
        # unclaimable for up to 24 hours. A live claim always has status
        # ``processing``; any token on another state is therefore orphaned.
        reset += session.execute(
            update(PlaudFile)
            .where(
                PlaudFile.status != FileStatus.processing,
                PlaudFile.processing_token.is_not(None),
            )
            .values(processing_token=None, processing_lease_until=None)
        ).rowcount
        session.execute(
            update(StageRun)
            .where(
                StageRun.file_id.in_(processing_ids),
                StageRun.status == StageStatus.running,
            )
            .values(
                status=StageStatus.failed,
                error=interruption,
                completed_at=now,
                updated_at=now,
            )
        )
        session.execute(
            update(StageAttempt)
            .where(
                StageAttempt.file_id.in_(processing_ids),
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
            .values(status=FileStatus.discovered, error=None)
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
    dest_dir = file_dir(file_id)
    dto = PlaudFileDTO.model_validate(raw or {"id": file_id})
    try:
        if not claim_acquired:
            with session_scope() as session:
                claimed = session.execute(
                    update(PlaudFile)
                    .where(
                        PlaudFile.id == file_id,
                        PlaudFile.status == FileStatus.discovered,
                    )
                    .values(status=FileStatus.downloading)
                ).rowcount
            if claimed != 1:
                return False
        else:
            with session_scope() as session:
                row = session.get(PlaudFile, file_id)
                if row is None or row.status != FileStatus.downloading:
                    return False
        dest = client.download_audio(dto, dest_dir)
        with session_scope() as session:
            fresh = session.get(PlaudFile, file_id)
            fresh.audio_path = str(dest)
            fresh.status = FileStatus.downloaded
            from datetime import datetime

            fresh.downloaded_at = datetime.now(UTC)
            fresh.error = None
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
            fresh = session.get(PlaudFile, file_id)
            fresh.status = FileStatus.error
            fresh.error = str(exc)[:2000]
        return False


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

    workers = max(1, settings.poller.max_concurrent_downloads)
    if workers == 1:
        return sum(_download_one(client, fid, raw, settings) for fid, raw in pending)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda fr: _download_one(client, fr[0], fr[1], settings), pending)
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
    summary_md = client.get_cloud_summary_md(file_id, detail)
    segments = client.get_cloud_transcript_segments(file_id, detail)
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
        if summary_md:
            title = next(
                (line[2:].strip() for line in summary_md.splitlines() if line.startswith("# ")),
                None,
            )
            session.add(
                SummaryRow(
                    file_id=file_id,
                    template="plaud",
                    title=title,
                    content_md=summary_md,
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
        row.cloud_is_summary = bool(summary_md)
        row.cloud_is_trans = bool(segments)
        row.raw = {
            **(row.raw or {}),
            _ARTIFACT_CHECKED_KEY: row.filename,
        }
    return (bool(segments), bool(summary_md))


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
    if settings.poller.auto_download:
        reset_download_errors()
    with make_plaud_client(settings.plaud) as client:
        new, changed = sync_file_list(client, settings)
        downloaded = download_pending(client, settings) if settings.poller.auto_download else 0
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
