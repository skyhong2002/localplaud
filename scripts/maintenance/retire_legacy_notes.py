"""Delete notes written under retired note-template keys (operator-authorized).

Dry run by default. ``--apply`` first writes a full SQLite backup and a JSON export of
every note it will remove, then deletes only notes under the named retired keys for
recordings that already have a note under a current catalog template. The note's
archived versions go with it, editable copies keep their text, and nothing else is
touched. Run it while the daemon is idle or accept a short wait on database locks.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from localplaud.config import get_settings
from localplaud.db.models import Summary, SummaryRevision
from localplaud.db.session import session_scope
from localplaud.note_history import legacy_note_templates, retire_legacy_notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template", action="append", required=True, help="retired key, e.g. default"
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    only = set(args.template)

    with session_scope() as session:
        legacy = legacy_note_templates(session)
        unknown = only - legacy
        if unknown:
            print(
                f"not retired template keys (current or absent): {sorted(unknown)}", file=sys.stderr
            )
            return 2
        targets = list(
            session.scalars(
                select(Summary.file_id)
                .where(Summary.source == "local", Summary.template.in_(only))
                .distinct()
            )
        )
        print(f"recordings with a note under {sorted(only)}: {len(targets)}")
        if not args.apply:
            print("dry run; pass --apply to delete")
            return 0

    settings = get_settings()
    db_path = Path(settings.store.database_url.removeprefix("sqlite:///"))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"localplaud-before-retire-{'-'.join(sorted(only))}-{stamp}.db"
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(backup)
    with target:
        source.backup(target)
    source.close()
    target.close()
    backup.chmod(0o600)
    print(f"backup written: {backup} ({backup.stat().st_size} bytes)")

    export = backup_dir / f"retired-notes-{'-'.join(sorted(only))}-{stamp}.json"
    removed = 0
    exported: list[dict] = []
    with session_scope() as session:
        for row in session.scalars(
            select(Summary).where(Summary.source == "local", Summary.template.in_(only))
        ):
            exported.append(
                {
                    "kind": "note",
                    "file_id": row.file_id,
                    "template": row.template,
                    "title": row.title,
                    "content_md": row.content_md,
                    "provider": row.llm_provider,
                    "model": row.model,
                    "created_at": str(row.created_at),
                }
            )
        for version in session.scalars(
            select(SummaryRevision).where(
                SummaryRevision.source == "local", SummaryRevision.template.in_(only)
            )
        ):
            exported.append(
                {
                    "kind": "version",
                    "file_id": version.file_id,
                    "template": version.template,
                    "revision": version.revision,
                    "title": version.title,
                    "content_md": version.content_md,
                }
            )
    export.write_text(json.dumps(exported, ensure_ascii=False, indent=1))
    export.chmod(0o600)
    print(f"export written: {export} ({len(exported)} rows)")

    skipped = 0
    for file_id in targets:
        with session_scope() as session:
            retired = retire_legacy_notes(session, file_id, keep_history=False, only=only)
        if retired:
            removed += len(retired)
        else:
            skipped += 1
    print(f"deleted notes: {removed}; recordings skipped (no current-template note): {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
