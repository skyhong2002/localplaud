"""Consistent, secret-excluding workspace backup archives."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
import zipfile
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from sqlalchemy.engine import make_url

from .config import get_settings

_BACKUP_RE = re.compile(r"^localplaud-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\.zip$")
_BACKUP_LOCK = threading.Lock()
_STORED_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm"}


def _database_path() -> Path:
    url = make_url(get_settings().store.database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        raise ValueError("workspace backup currently requires a file-backed SQLite database")
    return Path(url.database).expanduser().resolve()


def backup_root() -> Path:
    root = _database_path().parent / "backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_backup_path(name: str) -> Path:
    if not _BACKUP_RE.fullmatch(name):
        raise ValueError("invalid backup name")
    return backup_root() / name


def _package_version() -> str:
    try:
        return version("localplaud")
    except PackageNotFoundError:
        return "development"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _media_files(root: Path) -> tuple[list[Path], int]:
    files: list[Path] = []
    total = 0
    if not root.is_dir():
        return files, total
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        files.append(candidate)
        total += candidate.stat().st_size
    return files, total


def create_workspace_backup(*, include_media: bool = False) -> dict:
    """Create an online SQLite snapshot and optional local-media archive."""
    if not _BACKUP_LOCK.acquire(blocking=False):
        raise RuntimeError("another workspace backup is already running")
    try:
        database = _database_path()
        if not database.is_file():
            raise ValueError("workspace database does not exist")
        created_at = datetime.now(UTC)
        name = f"localplaud-{created_at:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}.zip"
        destination = _safe_backup_path(name)
        temporary = destination.with_suffix(".partial")
        snapshot = destination.with_suffix(".db.partial")
        media_root = Path(get_settings().poller.download_dir).expanduser().resolve()
        media_files, media_bytes = _media_files(media_root) if include_media else ([], 0)
        try:
            with sqlite3.connect(database) as source, sqlite3.connect(snapshot) as target:
                source.backup(target)
            manifest = {
                "schema": "localplaud-workspace-backup/v1",
                "created_at": created_at.isoformat(),
                "localplaud_version": _package_version(),
                "database": {
                    "archive_path": "database/localplaud.db",
                    "bytes": snapshot.stat().st_size,
                    "sha256": file_sha256(snapshot),
                },
                "media": {
                    "included": include_media,
                    "root": "media" if include_media else None,
                    "files": len(media_files),
                    "bytes": media_bytes,
                },
                "excluded": [
                    ".env and process environment variables",
                    "config.toml",
                    "Plaud OAuth token files",
                    "reverse-proxy credentials",
                    "provider and integration secret values",
                ],
                "restore": "Stop localplaud and follow docs/backups.md before replacing data.",
            }
            with zipfile.ZipFile(temporary, "w", allowZip64=True) as archive:
                archive.write(snapshot, "database/localplaud.db", zipfile.ZIP_DEFLATED)
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    compress_type=zipfile.ZIP_DEFLATED,
                )
                for path in media_files:
                    relative = path.relative_to(media_root)
                    compression = (
                        zipfile.ZIP_STORED
                        if path.suffix.casefold() in _STORED_SUFFIXES
                        else zipfile.ZIP_DEFLATED
                    )
                    archive.write(path, Path("media") / relative, compression)
            temporary.replace(destination)
            checksum = file_sha256(destination)
            destination.with_suffix(".zip.sha256").write_text(
                f"{checksum}  {destination.name}\n", encoding="ascii"
            )
            return manifest | {
                "name": destination.name,
                "size_bytes": destination.stat().st_size,
                "sha256": checksum,
            }
        finally:
            temporary.unlink(missing_ok=True)
            snapshot.unlink(missing_ok=True)
    finally:
        _BACKUP_LOCK.release()


def _read_manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        value = json.loads(archive.read("manifest.json"))
    if value.get("schema") != "localplaud-workspace-backup/v1":
        raise ValueError("unsupported backup manifest")
    return value


def list_workspace_backups() -> list[dict]:
    rows = []
    for path in sorted(backup_root().glob("localplaud-*.zip"), reverse=True)[:100]:
        if not _BACKUP_RE.fullmatch(path.name):
            continue
        try:
            manifest = _read_manifest(path)
            checksum_path = path.with_suffix(".zip.sha256")
            recorded = checksum_path.read_text(encoding="ascii").split()[0]
            rows.append(
                {
                    "name": path.name,
                    "created_at": manifest["created_at"],
                    "size_bytes": path.stat().st_size,
                    "sha256": recorded,
                    "media": manifest["media"],
                    "status": "ready",
                }
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile):
            rows.append({"name": path.name, "size_bytes": path.stat().st_size, "status": "invalid"})
    return rows


def workspace_backup_path(name: str) -> Path:
    path = _safe_backup_path(name)
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def delete_workspace_backup(name: str) -> None:
    path = workspace_backup_path(name)
    path.unlink()
    path.with_suffix(".zip.sha256").unlink(missing_ok=True)
