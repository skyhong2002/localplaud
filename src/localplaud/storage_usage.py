"""Measured local storage use and the derived-data retention policy.

Retention only ever touches derived copies (currently: workspace backup
archives). Original audio, the database, and user edits are never candidates.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from .config import get_settings
from .db.models import KeyValue, PlaudFile
from .db.tenancy import workspace_key

RETENTION_KEY = "storage_retention"
DEFAULT_RETENTION = {"backup_keep_latest": None}
MAX_BACKUP_KEEP = 100


def get_retention_policy(session: Session) -> dict:
    row = session.get(KeyValue, workspace_key(RETENTION_KEY))
    stored = row.value if row is not None and isinstance(row.value, dict) else {}
    return DEFAULT_RETENTION | {k: v for k, v in stored.items() if k in DEFAULT_RETENTION}


def save_retention_policy(session: Session, values: dict) -> dict:
    keep = values.get("backup_keep_latest")
    if keep is not None:
        keep = int(keep)
        if not 1 <= keep <= MAX_BACKUP_KEEP:
            raise ValueError(f"backup_keep_latest must be between 1 and {MAX_BACKUP_KEEP}")
    policy = DEFAULT_RETENTION | {"backup_keep_latest": keep}
    row = session.get(KeyValue, workspace_key(RETENTION_KEY))
    if row is None:
        session.add(KeyValue(key=workspace_key(RETENTION_KEY), value=policy))
    else:
        row.value = policy
    session.flush()
    return policy


def _size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() and not path.is_symlink() else 0
    except OSError:
        return 0


def _tree_size(root: Path) -> int:
    if not root.is_dir():
        return 0
    total = 0
    for candidate in root.rglob("*"):
        total += _size(candidate)
    return total


def _database_files() -> list[Path] | None:
    url = make_url(get_settings().store.database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        return None
    path = Path(url.database).expanduser().resolve()
    return [path, Path(f"{path}-wal"), Path(f"{path}-shm")]


def storage_usage(session: Session) -> dict:
    """Return byte counts per category; ``None`` means not measurable here."""
    from .backups import backup_root, list_workspace_backups

    original_bytes = converted_bytes = 0
    original_count = converted_count = 0
    seen: set[Path] = set()
    for audio_path, wav_path in session.execute(
        select(PlaudFile.audio_path, PlaudFile.wav_path)
    ):
        for value, kind in ((audio_path, "original"), (wav_path, "converted")):
            if not value:
                continue
            path = Path(value).expanduser()
            if path in seen:
                continue
            seen.add(path)
            size = _size(path)
            if not size:
                continue
            if kind == "original":
                original_bytes += size
                original_count += 1
            else:
                converted_bytes += size
                converted_count += 1

    download_dir = get_settings().poller.download_dir.expanduser()
    download_total = _tree_size(download_dir)
    database_files = _database_files()
    database_bytes = sum(_size(path) for path in database_files) if database_files else None
    try:
        backups = list_workspace_backups()
        backup_bytes = sum(
            _size(path) for path in backup_root().glob("localplaud-*.zip*")
        )
    except ValueError:
        backups, backup_bytes = [], None
    return {
        "original_audio": {"bytes": original_bytes, "files": original_count},
        "converted_audio": {"bytes": converted_bytes, "files": converted_count},
        "other_audio_dir": {"bytes": max(download_total - original_bytes - converted_bytes, 0)},
        "database": {"bytes": database_bytes},
        "backups": {"bytes": backup_bytes, "files": len(backups)},
    }


def retention_plan(session: Session, proposed: dict | None = None) -> dict:
    """Backups the saved (or proposed) policy would remove beyond the limit."""
    from .backups import list_workspace_backups

    policy = get_retention_policy(session) if proposed is None else DEFAULT_RETENTION | proposed
    keep = policy["backup_keep_latest"]
    # Stored values are re-validated: an out-of-range limit never deletes anything.
    if isinstance(keep, bool) or not isinstance(keep, int) or not 1 <= keep <= MAX_BACKUP_KEEP:
        return {"policy": policy, "backups_to_delete": []}
    try:
        backups = list_workspace_backups()  # newest first
    except ValueError:
        return {"policy": policy, "backups_to_delete": []}
    # The newest archive that includes media is kept even beyond the limit, so
    # database-only backups can never push out the only full copy.
    newest_media = next(
        (item["name"] for item in backups if (item.get("media") or {}).get("included")), None
    )
    doomed = [
        {"name": item["name"], "size_bytes": item.get("size_bytes", 0)}
        for item in backups[keep:]
        if item["name"] != newest_media
    ]
    return {"policy": policy, "backups_to_delete": doomed}


def apply_retention(session: Session, confirmed_names: list[str]) -> dict:
    """Delete only backups that are both in the current plan and confirmed."""
    from .backups import delete_workspace_backup

    planned = {item["name"] for item in retention_plan(session)["backups_to_delete"]}
    deleted: list[str] = []
    for name in confirmed_names:
        if name not in planned:
            continue
        try:
            delete_workspace_backup(name)
        except (OSError, ValueError):
            continue
        deleted.append(name)
    return {"deleted": deleted}


def enforce_retention_after_backup(session: Session) -> list[str]:
    """Apply the saved backup limit right after a new backup succeeds."""
    plan = retention_plan(session)
    return apply_retention(session, [item["name"] for item in plan["backups_to_delete"]])[
        "deleted"
    ]
