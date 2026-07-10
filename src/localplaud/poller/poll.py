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
from ..plaud import make_plaud_client
from ..plaud.models import PlaudFileDTO
from ..store.files import file_dir

log = logging.getLogger(__name__)

# Stash key inside PlaudFile.raw recording the filename at the last cloud-
# artifact check, so a rename (Plaud retitles a file when it summarizes it)
# triggers exactly one re-check instead of one per poll cycle.
_ARTIFACT_CHECKED_KEY = "_artifact_checked_name"


def _apply_dto(row: PlaudFile, dto: PlaudFileDTO) -> None:
    """Copy DTO fields onto the row. Only fields the provider actually sent
    are applied (the Open API omits version/md5/trash — those must not clobber
    values enriched from api-apse1)."""
    provided = dto.model_fields_set
    row.filename = dto.filename or row.filename
    if "fullname" in provided:
        row.fullname = dto.fullname
    if "filesize" in provided:
        row.filesize = dto.filesize
    if "file_md5" in provided:
        row.file_md5 = dto.file_md5
    if "duration" in provided:
        row.duration_ms = dto.duration
    if "start_time" in provided:
        row.start_time_ms = dto.start_time
    if "end_time" in provided:
        row.end_time_ms = dto.end_time
    if "scene" in provided:
        row.scene = dto.scene
    if "is_trash" in provided:
        row.is_trash = dto.is_trash
    if "version" in provided:
        row.version = dto.version
    if "version_ms" in provided:
        row.version_ms = dto.version_ms
    if "edit_time" in provided:
        row.edit_time = dto.edit_time
    if "is_trans" in provided:
        row.cloud_is_trans = dto.is_trans
    if "is_summary" in provided:
        row.cloud_is_summary = dto.is_summary
    row.raw = {**(row.raw or {}), **dto.model_dump(exclude_unset=True)}


def sync_file_list(client, settings: Settings) -> tuple[int, int]:
    """Upsert the cloud listing. Returns (new_count, changed_count).

    Works with either provider; both use the same file ids, so running this
    once with the official client and again with the api-apse1 client (see
    ``enrich_from_apse1``) merges cleanly into the same rows.
    """
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
                # Version-based change detection only applies when the provider
                # sends version fields (the Open API doesn't — "unknown" must
                # not read as "changed").
                provided = dto.model_fields_set
                changed = ("version" in provided or "version_ms" in provided) and (
                    (dto.version_ms or 0) != (row.version_ms or 0)
                    or (dto.version or 0) != (row.version or 0)
                )
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


def _download_one(client, file_id: str, raw: dict, settings: Settings) -> bool:
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
        if settings.pipeline.prefer_cloud_artifacts:
            # The official client already fetched (and cached) the detail
            # payload for the presigned URL — mirroring Plaud's own
            # transcript/summary here is nearly free and lets the pipeline
            # skip local re-transcription.
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
        return sum(_download_one(client, fid, raw, settings) for fid, raw in pending)

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda fr: _download_one(client, fr[0], fr[1], settings), pending)
    return sum(results)


def enrich_from_apse1(settings: Settings) -> tuple[int, int]:
    """Optional second listing pass through the reverse-engineered api-apse1
    client, filling the change-detection fields the Open API lacks
    (``version``/``file_md5``/``edit_time``/``is_trash``, scene, cloud flags).

    Both providers use the same file ids, so this is just ``sync_file_list``
    with the legacy client. Only runs when the primary provider is
    ``official``, enrichment is enabled, and apse1 credentials are configured;
    failures degrade to a warning (the official sync already succeeded)."""
    cfg = settings.plaud
    if cfg.provider != "official" or not cfg.apse1_enrichment:
        return (0, 0)
    if not (cfg.token or cfg.cookie):
        return (0, 0)
    from ..plaud.client import PlaudClient

    try:
        with PlaudClient(cfg) as legacy:
            return sync_file_list(legacy, settings)
    except Exception as exc:  # noqa: BLE001
        log.warning("apse1 enrichment failed (continuing without it): %s", exc)
        return (0, 0)


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
        has_transcript = row.transcript is not None

    stored = False
    md = None if has_summary else client.get_cloud_summary_md(file_id)
    if md:
        title = next((ln[2:].strip() for ln in md.splitlines() if ln.startswith("# ")), None)
        with session_scope() as session:
            session.add(
                SummaryRow(
                    file_id=file_id, template="plaud", title=title,
                    content_md=md, source="cloud",
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
        stored = True

    # Remember which filename this check ran under, so we only re-check after
    # a rename instead of on every poll cycle.
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        row.raw = {**(row.raw or {}), _ARTIFACT_CHECKED_KEY: filename}
    return stored


def ingest_cloud_artifacts(client, settings: Settings) -> int:
    """Mirror Plaud's own transcript (with speakers) and summary (markdown)
    for files that have them, stored as ``source="cloud"`` rows. The pipeline
    reuses a mirrored transcript, skipping local re-transcription entirely.

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
            missing_transcript = r.cloud_is_trans and r.transcript is None
            renamed = (
                bool(r.filename)
                and not _looks_like_raw_name(r.filename)
                and (r.raw or {}).get(_ARTIFACT_CHECKED_KEY) != r.filename
                and (not any(s.template == "plaud" for s in r.summaries) or r.transcript is None)
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
    """One full poll cycle: sync listing (+ optional apse1 enrichment) +
    download pending (+ mirror Plaud's own transcripts/summaries when
    ``pipeline.prefer_cloud_artifacts`` is set)."""
    settings = settings or get_settings()
    reset_inflight()
    reset_download_errors()
    with make_plaud_client(settings.plaud) as client:
        new, changed = sync_file_list(client, settings)
        enriched_new, enriched_changed = enrich_from_apse1(settings)
        downloaded = download_pending(client, settings)
        cloud_artifacts = (
            ingest_cloud_artifacts(client, settings)
            if settings.pipeline.prefer_cloud_artifacts
            else 0
        )
    result = {"new": new + enriched_new, "changed": changed + enriched_changed,
              "downloaded": downloaded, "cloud_artifacts": cloud_artifacts}
    log.info("Poll cycle complete: %s", result)
    return result
