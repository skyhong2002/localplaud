"""Unwrap notes whose body was stored as a JSON envelope (operator-authorized).

Two envelopes ended up in ``summaries.content_md`` verbatim and render as raw JSON:

* cloud mirrors of Plaud v2 summaries: ``{"ai_content": "<markdown>", "category": ...}``
* early local Ollama notes: ``{"title": "...", "content_md": "<markdown>"}``

Dry run by default. ``--apply`` first writes a full SQLite backup and a JSON export of
every original body it will rewrite, then replaces only ``content_md`` (and a missing or
JSON-quoted ``title`` for local notes). Local notes are archived as a note version first
so the original stays restorable; cloud mirrors are replaced in place (the export keeps
the original). Rows that are not exactly one of the envelopes above are left alone, and
a local envelope whose inner body is not usable Markdown (empty, an unrendered
transcript, or cut off mid-string by the model's output limit) is reported for
regeneration instead of being rewritten. Run it while the daemon is idle or
accept a short wait on database locks.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from localplaud.config import get_settings
from localplaud.db.models import Summary
from localplaud.db.session import session_scope
from localplaud.note_history import archive_summary
from localplaud.plaud.official import _normalize_cloud_markdown, _unwrap_cloud_note_body

_REASON = "unwrap-json-envelope"
_TRANSCRIPT_LINE = ("SPEAKER_", "Speaker ")


def _load(raw: str) -> dict | None:
    stripped = (raw or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def plan(row: Summary) -> tuple[str, str, str | None] | None:
    """Return ``(kind, new_content, new_title)`` for a rewritable row, a
    ``("review", reason, None)`` marker for one needing regeneration, or None."""
    if row.source in {"cloud", "plaud"}:
        body = _unwrap_cloud_note_body(row.content_md)
        if body == row.content_md:
            return None
        body = _normalize_cloud_markdown(body)
        if not body:
            return ("review", "empty ai_content", None)
        return ("cloud", body, None)
    if row.source == "local":
        data = _load(row.content_md)
        if data is None:
            # Early small-model runs hit the output limit mid-string: the envelope is
            # unterminated, so the note body is cut off and cannot be recovered.
            if row.content_md.lstrip().startswith("{") and '"content_md"' in row.content_md:
                return ("review", "truncated JSON envelope; note body is incomplete", None)
            return None
        if not isinstance(data.get("content_md"), str) or "ai_content" in data:
            return None
        body = data["content_md"].strip()
        if not body:
            return ("review", "empty content_md", None)
        if body.startswith(_TRANSCRIPT_LINE):
            return ("review", "inner body is transcript text, not a note", None)
        title = data.get("title")
        return ("local", body, title.strip() if isinstance(title, str) and title.strip() else None)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    with session_scope() as session:
        rows = list(session.scalars(select(Summary).order_by(Summary.file_id, Summary.id)))
        planned = [(row.id, row.file_id, row.template, row.source, plan(row)) for row in rows]
    planned = [item for item in planned if item[4] is not None]
    rewrite = [item for item in planned if item[4][0] != "review"]
    review = [item for item in planned if item[4][0] == "review"]
    print(
        f"rewritable: {len(rewrite)} "
        f"(cloud {sum(i[4][0] == 'cloud' for i in rewrite)}, "
        f"local {sum(i[4][0] == 'local' for i in rewrite)}); "
        f"needs regeneration instead: {len(review)}"
    )
    for sid, file_id, template, source, (_kind, detail, _title) in review:
        print(f"  review  {file_id} {source}/{template} (id {sid}): {detail}")
    if not args.apply:
        print("dry run; pass --apply to rewrite")
        return 0
    if not rewrite:
        return 0

    settings = get_settings()
    db_path = Path(settings.store.database_url.removeprefix("sqlite:///"))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"localplaud-before-unwrap-json-notes-{stamp}.db"
    source_db = sqlite3.connect(db_path)
    target_db = sqlite3.connect(backup)
    with target_db:
        source_db.backup(target_db)
    source_db.close()
    target_db.close()
    backup.chmod(0o600)
    print(f"backup written: {backup} ({backup.stat().st_size} bytes)")

    export = backup_dir / f"unwrapped-notes-{stamp}.json"
    originals: list[dict] = []
    with session_scope() as session:
        for sid, *_ in rewrite:
            row = session.get(Summary, sid)
            originals.append(
                {
                    "summary_id": row.id,
                    "file_id": row.file_id,
                    "template": row.template,
                    "source": row.source,
                    "title": row.title,
                    "content_md": row.content_md,
                }
            )
    export.write_text(json.dumps(originals, ensure_ascii=False, indent=1))
    export.chmod(0o600)
    print(f"export written: {export} ({len(originals)} rows)")

    done = 0
    for sid, _file_id, _template, _source, (kind, body, title) in rewrite:
        with session_scope() as session:
            row = session.get(Summary, sid)
            if row is None or plan(row) is None:  # changed since the dry run
                continue
            if kind == "local":
                archive_summary(session, row, reason=_REASON)
                if title:
                    row.title = title
            row.content_md = body
        done += 1
    print(f"rewritten notes: {done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
