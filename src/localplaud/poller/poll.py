"""The poller — mirror the Plaud cloud into the local store.

Two steps, both read-only against the cloud:

1. ``sync_file_list`` — pull the file listing and upsert rows, marking new or
   changed files (by ``version``/``version_ms``) for (re)processing.
2. ``download_pending`` — download audio for files that need it.
"""

from __future__ import annotations

import logging
from datetime import UTC

from sqlalchemy import select

from ..config import Settings, get_settings
from ..db.models import FileStatus, PlaudFile
from ..db.session import session_scope
from ..plaud.client import PlaudClient
from ..plaud.models import PlaudFileDTO
from ..store.files import file_dir

log = logging.getLogger(__name__)


def _apply_dto(row: PlaudFile, dto: PlaudFileDTO) -> None:
    row.filename = dto.filename or row.filename
    row.fullname = dto.fullname
    row.filesize = dto.filesize
    row.file_md5 = dto.file_md5
    row.duration_ms = dto.duration
    row.start_time_ms = dto.start_time
    row.end_time_ms = dto.end_time
    row.scene = dto.scene
    row.is_trash = dto.is_trash
    row.version = dto.version
    row.version_ms = dto.version_ms
    row.edit_time = dto.edit_time
    row.cloud_is_trans = dto.is_trans
    row.cloud_is_summary = dto.is_summary
    row.raw = dto.model_dump()


def sync_file_list(client: PlaudClient, settings: Settings) -> tuple[int, int]:
    """Upsert the cloud listing. Returns (new_count, changed_count)."""
    new_count = changed_count = 0
    with session_scope() as session:
        for dto in client.iter_files(include_trash=settings.poller.include_trash):
            row = session.get(PlaudFile, dto.id)
            if row is None:
                row = PlaudFile(id=dto.id, status=FileStatus.discovered)
                _apply_dto(row, dto)
                session.add(row)
                new_count += 1
                log.info("New file discovered: %s (%s)", dto.id, dto.filename)
            else:
                # Capture the pre-update values — _apply_dto overwrites them.
                changed = (dto.version_ms or 0) != (row.version_ms or 0) or (
                    dto.version or 0
                ) != (row.version or 0)
                md5_changed = bool(dto.file_md5) and dto.file_md5 != row.file_md5
                _apply_dto(row, dto)
                # Re-process a finished/errored file when the cloud changed it.
                # An in-flight row (downloading/processing) is left alone.
                if (changed or md5_changed) and row.status in (
                    FileStatus.done,
                    FileStatus.error,
                ):
                    if md5_changed or not row.audio_path:
                        # The audio itself changed — force a fresh download.
                        row.status = FileStatus.discovered
                    else:
                        row.status = FileStatus.downloaded
                    changed_count += 1
                    log.info("File changed upstream, will reprocess: %s", dto.id)
    return new_count, changed_count


def reset_inflight() -> int:
    """Recover files stranded mid-flight by a crash/kill: ``downloading`` →
    ``discovered`` and ``processing`` → ``downloaded``. Safe to call at the
    start of every cycle. Returns the number of rows reset."""
    from sqlalchemy import update

    reset = 0
    with session_scope() as session:
        reset += session.execute(
            update(PlaudFile)
            .where(PlaudFile.status == FileStatus.downloading)
            .values(status=FileStatus.discovered)
        ).rowcount
        reset += session.execute(
            update(PlaudFile)
            .where(PlaudFile.status == FileStatus.processing)
            .values(status=FileStatus.downloaded)
        ).rowcount
    if reset:
        log.info("Reset %d in-flight file(s) after restart", reset)
    return reset


def _download_one(client: PlaudClient, file_id: str, raw: dict) -> bool:
    dest_dir = file_dir(file_id)
    dto = PlaudFileDTO.model_validate(raw or {"id": file_id})
    try:
        with session_scope() as session:
            session.get(PlaudFile, file_id).status = FileStatus.downloading
        dest = client.download_audio(dto, dest_dir)
        with session_scope() as session:
            fresh = session.get(PlaudFile, file_id)
            fresh.audio_path = str(dest)
            fresh.status = FileStatus.downloaded
            from datetime import datetime

            fresh.downloaded_at = datetime.now(UTC)
            fresh.error = None
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Download failed for %s: %s", file_id, exc)
        with session_scope() as session:
            fresh = session.get(PlaudFile, file_id)
            fresh.status = FileStatus.error
            fresh.error = str(exc)[:2000]
        return False


def download_pending(client: PlaudClient, settings: Settings) -> int:
    """Download audio for discovered files, up to
    ``poller.max_concurrent_downloads`` at a time. Returns count downloaded."""
    with session_scope() as session:
        stmt = select(PlaudFile.id, PlaudFile.raw).where(
            PlaudFile.status == FileStatus.discovered
        )
        if not settings.poller.include_trash:
            stmt = stmt.where(PlaudFile.is_trash.is_(False))
        pending = [(fid, raw) for fid, raw in session.execute(stmt)]
    if not pending:
        return 0

    workers = max(1, settings.poller.max_concurrent_downloads)
    if workers == 1:
        return sum(_download_one(client, fid, raw) for fid, raw in pending)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda fr: _download_one(client, fr[0], fr[1]), pending)
    return sum(results)


def ingest_cloud_summaries(client: PlaudClient, settings: Settings) -> int:
    """Mirror Plaud's own summary (markdown) for files that have one, stored as
    a ``source="cloud"`` summary under the ``plaud`` template. Lets even a box
    with no local ASR/LLM keep the cloud's notes. Returns count ingested."""
    from ..db.models import Summary as SummaryRow

    ingested = 0
    with session_scope() as session:
        stmt = select(PlaudFile).where(PlaudFile.cloud_is_summary.is_(True))
        candidates = [(r.id) for r in session.scalars(stmt)]

    for fid in candidates:
        with session_scope() as session:
            has_cloud = any(s.template == "plaud" for s in session.get(PlaudFile, fid).summaries)
        if has_cloud:
            continue
        try:
            md = client.get_cloud_summary_md(fid)
        except Exception as exc:  # noqa: BLE001
            log.warning("cloud summary fetch failed for %s: %s", fid, exc)
            continue
        if not md:
            continue
        title = next((ln[2:].strip() for ln in md.splitlines() if ln.startswith("# ")), None)
        with session_scope() as session:
            session.add(
                SummaryRow(
                    file_id=fid, template="plaud", title=title,
                    content_md=md, source="cloud",
                )
            )
        ingested += 1
    return ingested


def poll_once(settings: Settings | None = None) -> dict:
    """One full poll cycle: sync listing + download pending (+ optionally mirror
    Plaud's own summaries when ``pipeline.prefer_cloud_artifacts`` is set)."""
    settings = settings or get_settings()
    reset_inflight()
    with PlaudClient(settings.plaud) as client:
        new, changed = sync_file_list(client, settings)
        downloaded = download_pending(client, settings)
        cloud_summaries = (
            ingest_cloud_summaries(client, settings)
            if settings.pipeline.prefer_cloud_artifacts
            else 0
        )
    result = {"new": new, "changed": changed, "downloaded": downloaded,
              "cloud_summaries": cloud_summaries}
    log.info("Poll cycle complete: %s", result)
    return result
