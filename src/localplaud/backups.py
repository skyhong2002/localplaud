"""Consistent, secret-excluding workspace backup archives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import uuid
import zipfile
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath

from sqlalchemy.engine import make_url

from .config import get_settings

_BACKUP_RE = re.compile(r"^localplaud-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\.zip$")
_BACKUP_LOCK = threading.Lock()
_STORED_SUFFIXES = {
    ".aac",
    ".amr",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}


def _database_path() -> Path:
    url = make_url(get_settings().store.database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        raise ValueError("workspace backup currently requires a file-backed SQLite database")
    return Path(url.database).expanduser().resolve()


def backup_root() -> Path:
    root = _database_path().parent / "backups"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def _media_files(root: Path, snapshot: Path) -> tuple[list[Path], list[dict]]:
    """Inventory only recordings present in the database snapshot, never new uploads."""
    with closing(sqlite3.connect(snapshot)) as connection:
        rows = connection.execute("SELECT id, audio_path, wav_path FROM plaud_files").fetchall()
    files: set[Path] = set()
    references = []
    for file_id, audio, wav in rows:
        for column, value in (("audio_path", audio), ("wav_path", wav)):
            if not value:
                continue
            # Do not resolve symlinks here: opening below must reject them.
            path = Path(os.path.abspath(Path(value).expanduser()))
            try:
                relative = path.relative_to(root)
            except ValueError:
                raise ValueError(
                    "snapshot references media outside the configured media directory"
                ) from None
            files.add(relative)
            references.append(
                {
                    "file_id": file_id,
                    "column": column,
                    "archive_path": f"media/{relative.as_posix()}",
                }
            )
        # Include note assets and playback caches for snapshot recordings, excluding
        # unfinished downloads/conversions and quarantined deletions.
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", file_id):
            directory = root / file_id
            if directory.is_symlink():
                continue
            for parent, directories, names in os.walk(directory, followlinks=False):
                directories[:] = sorted(d for d in directories if not d.startswith("."))
                for name in sorted(names):
                    path = Path(parent) / name
                    if not name.startswith(".") and not path.is_symlink() and path.is_file():
                        files.add(path.relative_to(root))
    return sorted(files), references


def _open_media(root: Path, relative: Path):
    """Reject symlinks in every component, including replacements during inventory."""
    with ExitStack() as directories:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        directories.callback(os.close, fd)
        for part in relative.parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            directories.callback(os.close, fd)
        media_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    handle = os.fdopen(media_fd, "rb")
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise ValueError("snapshot media must be regular files")
    return handle


def _copy_media(archive: zipfile.ZipFile, relative: Path, handle, before) -> dict:
    digest = hashlib.sha256()
    size = 0
    compression = (
        zipfile.ZIP_STORED
        if relative.suffix.casefold() in _STORED_SUFFIXES
        else zipfile.ZIP_DEFLATED
    )
    member = zipfile.ZipInfo(f"media/{relative.as_posix()}")
    member.compress_type = compression
    member.external_attr = 0o100600 << 16
    with archive.open(member, "w", force_zip64=True) as output:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            output.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    after = os.fstat(handle.fileno())
    # Unlink/rename changes ctime, but a pinned descriptor still has the original
    # bytes. In-place writes change size/mtime and must fail instead of publishing.
    if size != before.st_size or (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RuntimeError("media changed during backup; retry the backup")
    return {"archive_path": member.filename, "bytes": size, "sha256": digest.hexdigest()}


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
        checksum_path = destination.with_suffix(".zip.sha256")
        checksum_temporary = destination.with_suffix(".sha256.partial")
        media_root = Path(get_settings().poller.download_dir).expanduser().resolve()
        with ExitStack() as pinned:
            try:
                snapshot.touch(mode=0o600, exist_ok=False)
                temporary.touch(mode=0o600, exist_ok=False)
                checksum_temporary.touch(mode=0o600, exist_ok=False)
                # Existing upload publication/eviction paths serialize through SQLite
                # writes. Hold that barrier only until the snapshot and descriptors are
                # captured, then allow writers while the potentially long copy runs.
                with closing(sqlite3.connect(database)) as barrier:
                    if include_media:
                        try:
                            barrier.execute("BEGIN IMMEDIATE")
                        except sqlite3.OperationalError as exc:
                            raise RuntimeError(
                                "workspace database is busy; retry the backup"
                            ) from exc
                    with (
                        closing(sqlite3.connect(database)) as source,
                        closing(sqlite3.connect(snapshot)) as target,
                    ):
                        source.backup(target)
                    media_files, references = (
                        _media_files(media_root, snapshot) if include_media else ([], [])
                    )
                    handles = []
                    for path in media_files:
                        handle = pinned.enter_context(_open_media(media_root, path))
                        handles.append((path, handle, os.fstat(handle.fileno())))
                    barrier.rollback()
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
                        "bytes": 0,
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
                    if include_media:
                        entries = [
                            _copy_media(archive, path, handle, before)
                            for path, handle, before in handles
                        ]
                        manifest["media"].update(
                            entries=entries,
                            references=references,
                            bytes=sum(entry["bytes"] for entry in entries),
                        )
                    archive.writestr(
                        "manifest.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        compress_type=zipfile.ZIP_DEFLATED,
                    )
                checksum = file_sha256(temporary)
                checksum_temporary.write_text(f"{checksum}  {destination.name}\n", encoding="ascii")
                # Catalog scans only completed ZIP names. Publish the checksum
                # first so a visible archive always has its sidecar ready.
                checksum_temporary.replace(checksum_path)
                temporary.replace(destination)
                return manifest | {
                    "name": destination.name,
                    "size_bytes": destination.stat().st_size,
                    "sha256": checksum,
                }
            finally:
                temporary.unlink(missing_ok=True)
                snapshot.unlink(missing_ok=True)
                checksum_temporary.unlink(missing_ok=True)
                if not destination.exists():
                    checksum_path.unlink(missing_ok=True)
    finally:
        _BACKUP_LOCK.release()


def _read_manifest(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        value = json.loads(archive.read("manifest.json"))
    if not isinstance(value, dict) or value.get("schema") != "localplaud-workspace-backup/v1":
        raise ValueError("unsupported backup manifest")
    return value


def restore_workspace_backup(
    archive_path: Path, destination: Path, *, expected_sha256: str
) -> dict:
    """Verify and restore into a NEW isolated directory; never load live settings.

    This offline helper deliberately requires a trusted, separately recorded
    archive digest. It does not replace an active installation or start workers.
    """
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or file_sha256(archive_path) != expected_sha256
    ):
        raise ValueError("backup archive checksum mismatch")
    manifest = _read_manifest(archive_path)
    media = manifest["media"]
    if not media.get("included") or "entries" not in media or "references" not in media:
        raise ValueError("isolated restore requires a full backup with per-file checksums")
    entries = [manifest["database"], *media["entries"]]
    paths = [entry["archive_path"] for entry in entries]
    if paths[0] != "database/localplaud.db" or len(paths) != len(set(paths)):
        raise ValueError("invalid backup inventory")
    for name in paths:
        relative = PurePosixPath(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in name
            or relative.as_posix() != name
            or (name != "database/localplaud.db" and not name.startswith("media/"))
        ):
            raise ValueError("unsafe backup member")
    if media["files"] != len(media["entries"]) or media["bytes"] != sum(
        entry["bytes"] for entry in media["entries"]
    ):
        raise ValueError("backup media totals mismatch")
    destination = destination.expanduser().absolute()
    # mkdir's exclusive behavior protects both existing data and symlink targets.
    destination.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != {*paths, "manifest.json"}:
                raise ValueError("backup inventory does not match archive members")
            for entry in entries:
                info = archive.getinfo(entry["archive_path"])
                if stat.S_ISLNK(info.external_attr >> 16) or info.file_size != entry["bytes"]:
                    raise ValueError("invalid backup member size or type")
                target = destination / entry["archive_path"]
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.touch(mode=0o600, exist_ok=False)
                digest = hashlib.sha256()
                with archive.open(info) as source, target.open("wb") as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        output.write(chunk)
                if digest.hexdigest() != entry["sha256"]:
                    raise ValueError("backup member checksum mismatch")
        database = destination / "database/localplaud.db"
        with closing(sqlite3.connect(database)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("backup database failed integrity check")
            rows = connection.execute("SELECT id, audio_path, wav_path FROM plaud_files").fetchall()
            expected = {
                (row[0], column)
                for row in rows
                for column, value in zip(("audio_path", "wav_path"), row[1:], strict=True)
                if value
            }
            references = media["references"]
            actual = {(ref["file_id"], ref["column"]) for ref in references}
            if expected != actual or len(actual) != len(references):
                raise ValueError("backup database media references mismatch")
            for ref in references:
                # column names are validated through the expected set above.
                if ref["archive_path"] not in paths[1:]:
                    raise ValueError("backup reference missing from media inventory")
                connection.execute(
                    f"UPDATE plaud_files SET {ref['column']}=? WHERE id=?",  # noqa: S608
                    (str(destination / ref["archive_path"]), ref["file_id"]),
                )
            connection.commit()
        (destination / "media").mkdir(exist_ok=True, mode=0o700)
        manifest_path = destination / "manifest.json"
        manifest_path.touch(mode=0o600, exist_ok=False)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return {"database": str(database), "media_root": str(destination / "media")}
    except BaseException:
        shutil.rmtree(destination)
        raise


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
        except (
            OSError,
            ValueError,
            KeyError,
            IndexError,
            json.JSONDecodeError,
            zipfile.BadZipFile,
        ):
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
