"""Plan and apply title-only repairs; keep the private JSONL as revision history.

Run with ``python -m localplaud.title_repair --help``. Planning invokes only the
recording's existing local/remote Ollama selection; applying never invokes an LLM.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text, update

from .config import get_settings
from .db.models import PlaudFile, Summary
from .db.session import session_scope
from .worker import pipeline, summarize
from .worker.title_policy import TITLE_PROMPT_VERSION


def _fingerprint(transcript) -> str:
    return hashlib.sha256(summarize._render_transcript(transcript).encode()).hexdigest()


def _before(row) -> dict:
    return {
        "title": row.generated_title,
        "provider": row.generated_title_provider,
        "model": row.generated_title_model,
        "at": row.generated_title_at.isoformat() if row.generated_title_at else None,
    }


def plan_repair(file_id: str, settings) -> dict:
    """Generate a candidate without changing any recording or processing state."""
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None or row.local_title or row.is_trash or row.processing_token:
            raise ValueError("recording missing, manually named, trashed, or processing")
        before = _before(row)
        note = session.scalar(
            select(Summary).where(
                Summary.file_id == file_id, Summary.source == "local",
                Summary.template != "mind_map", Summary.title == row.generated_title,
            ).order_by(Summary.created_at.desc()).limit(1)
        )
        if note is None or not note.resolved_profile_snapshot:
            raise ValueError("no matching local note with a resolved provider profile")
        snapshot = note.resolved_profile_snapshot
    # Never let a migration/import preference feed cloud artifacts into repair.
    independent = settings.model_copy(deep=True)
    independent.pipeline.artifact_mode = "independent"
    loaded = pipeline._load_transcript(file_id, independent)
    if loaded is None or loaded[1] != "local":
        raise ValueError("no canonical local transcript")
    transcript = loaded[0]
    lineage = pipeline._transcript_lineage(file_id, independent)
    selection = snapshot.get("stages", {}).get("summarize", {})
    if selection.get("provider_type") not in {"ollama", "localplaud-worker"}:
        raise ValueError("title repair supports existing local/remote Ollama profiles only")
    if pipeline._remote_selection(snapshot, "summarize"):
        result = pipeline._run_remote_stage(
            file_id, snapshot, "summarize",
            [pipeline._remote_json_input("transcript", pipeline._transcript_payload(transcript))],
            options={"title_only": True, "title_prompt_version": TITLE_PROMPT_VERSION},
        )
        if result.get("title_prompt_version") != TITLE_PROMPT_VERSION:
            raise ValueError("remote worker does not support this title-only contract")
    else:
        selected = pipeline._settings_for_stage(independent, snapshot, "summarize")
        provider, model = summarize._llm_provider_model(selected)
        result = {"title": summarize.generate_recording_title(transcript, selected),
                  "provider": provider, "model": model}
    title = pipeline._clean_generated_title(result.get("title"))
    if not title:
        raise ValueError("provider returned an invalid recording title")
    return {
        "schema": "localplaud-title-repair/v1", "file_id": file_id,
        "before": before, "title": title, "provider": result["provider"],
        "model": result["model"], "source": "local", "transcript": lineage,
        "transcript_sha256": _fingerprint(transcript),
        "prompt_version": TITLE_PROMPT_VERSION, "created_at": datetime.now(UTC).isoformat(),
    }


def apply_repair(item: dict, settings) -> bool:
    """Apply a reviewed candidate only while its title and source remain current."""
    if item.get("schema") != "localplaud-title-repair/v1" or item.get("source") != "local":
        raise ValueError("unsupported repair record")
    title = pipeline._clean_generated_title(item.get("title"))
    if not title:
        raise ValueError("invalid repair title")
    independent = settings.model_copy(deep=True)
    independent.pipeline.artifact_mode = "independent"
    with session_scope() as session:
        if session.bind.dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        row = session.get(PlaudFile, item["file_id"])
        if (row is None or row.local_title or row.is_trash or row.processing_token
                or _before(row) != item["before"]):
            return False
        # Keep the SQLite read transaction open through the guarded update. A
        # concurrent source edit cannot silently be committed over this snapshot.
        raw = pipeline._select_raw_transcript(row, independent)
        if raw is None or raw.source != "local":
            return False
        revision = row.corrected_transcript_for_source("local")
        lineage = {"input_transcript_id": raw.id,
                   "input_transcript_revision": revision.revision if revision else 0,
                   "input_transcript_source": "local"}
        transcript = (pipeline._rehydrate_revision(revision, raw) if revision
                      else pipeline._rehydrate_transcript(raw))
        transcript = pipeline._apply_speaker_display_names(
            transcript, {s.key: s.display_name for s in row.speakers if s.display_name}
        )
        if lineage != item["transcript"] or _fingerprint(transcript) != item["transcript_sha256"]:
            return False
        result = session.execute(
            update(PlaudFile).where(
                PlaudFile.id == row.id, PlaudFile.local_title.is_(None),
                PlaudFile.generated_title == row.generated_title,
                PlaudFile.generated_title_at == row.generated_title_at,
                PlaudFile.processing_token.is_(None),
            ).values(generated_title=title, generated_title_provider=item["provider"],
                     generated_title_model=item["model"], generated_title_at=datetime.now(UTC))
        )
        return result.rowcount == 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", type=Path, help="Private JSONL revision log; resumes if present")
    action.add_argument("--apply", type=Path, help="Apply reviewed plan without regenerating")
    parser.add_argument("--since", type=datetime.fromisoformat,
                        help="Also regenerate automatic titles created since this UTC time")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    settings = get_settings()
    if args.apply:
        applied = skipped = 0
        for line in args.apply.read_text().splitlines():
            if apply_repair(json.loads(line), settings):
                applied += 1
            else:
                skipped += 1
        print(f"Applied: {applied}; unchanged/stale: {skipped}")
        return
    path = args.plan
    path.parent.mkdir(parents=True, exist_ok=True)
    done = {json.loads(line)["file_id"] for line in path.read_text().splitlines()} if path.exists() else set()
    with session_scope() as session:
        ids = [row.id for row in session.scalars(select(PlaudFile).order_by(PlaudFile.id))
               if row.generated_title and not row.local_title and not row.is_trash
               and not row.processing_token and row.id not in done
               and (not pipeline._clean_generated_title(row.generated_title)
                    or (args.since and row.generated_title_at and row.generated_title_at >= args.since))]
    if args.limit:
        ids = ids[:args.limit]
    print(f"Planning {len(ids)} recording titles", flush=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as output:
        for index, file_id in enumerate(ids, 1):
            try:
                item = plan_repair(file_id, settings)
                output.write(json.dumps(item, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
                print(f"{index}/{len(ids)} planned", flush=True)
            except Exception as exc:
                # Providers can put private prompts in exception messages.
                print(f"{index}/{len(ids)} skipped: {type(exc).__name__}", flush=True)


if __name__ == "__main__":
    main()
