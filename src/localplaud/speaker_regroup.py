"""Re-split stored transcripts into clause-level speaker turns.

Aligners that omit punctuation (Qwen forced alignment, some Whisper tokens)
used to defeat speaker grouping: words could not be matched back to the
segment text, so a whole ASR window -- two minutes for Qwen -- kept a single
majority speaker. Stored words already carry their own speakers, so repair
needs no audio, ASR, or diarization rerun.

The raw transcript is regrouped in place, exactly as the diarize stage would
now persist it. Machine revisions are appended as regrouped copies whose
corrected wording is carried onto the new turns, so automatic correction is
not recharged. Human-edited transcripts are left alone. Notes, mind maps, and
indexes are marked out of date; ``apply --manifest`` writes a recovery
manifest for ``scripts/maintenance/repair_processing.py`` to regenerate them.

    python -m localplaud.speaker_regroup scan
    python -m localplaud.speaker_regroup apply --manifest private/regroup.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from .asr.base import Transcript
from .config import Settings, get_settings
from .db.models import FileStatus, PlaudFile, StageName, StageRun, TranscriptRevision
from .db.session import session_scope
from .worker.diarize import SPEAKER_TURN_STRATEGY, _word_spans, group_speaker_segments

_HUMAN_KINDS = frozenset({"user_edit", "restore", "speaker_edit"})
_ACOUSTIC_KINDS = frozenset({"speech_cleanup", "speech_retranscribe", "vocabulary"})
_POLISH_KINDS = frozenset({"ai_polish", "ai_polish_after_speech"})


def _signature(segment: dict) -> tuple:
    return tuple((w.get("start"), w.get("end")) for w in segment.get("words") or [])


def _speaker_seconds(segments: list[dict]) -> dict[tuple, str | None]:
    """Displayed speaker for every timed word, keyed by its timing."""
    return {
        (word.get("start"), word.get("end")): segment.get("speaker")
        for segment in segments
        for word in segment.get("words") or []
    }


def _reattributed_seconds(before: list[dict], after: list[dict]) -> float:
    old, new = _speaker_seconds(before), _speaker_seconds(after)
    return sum(
        max(0.0, (end or 0.0) - (start or 0.0))
        for (start, end), speaker in new.items()
        if old.get((start, end), speaker) != speaker
    )


def _aligned_source(raw: dict, segment: dict) -> str | None:
    """Raw ASR text spelled with ``segment``'s words, e.g. after script folding.

    Corrected revisions keep the aligned words but convert them character by
    character, which can differ from converting the whole text. Substituting
    each word's letters into its raw span keeps words and text consistent.
    """
    from .worker.pipeline import _rehydrate_segments

    source = _rehydrate_segments([raw])[0]
    words = segment.get("words") or []
    spans = _word_spans(source.text, source.words)
    if spans is None or len(words) != len(spans):
        return None
    text = list(source.text)
    for (start, end), word in zip(spans, words, strict=True):
        positions = [index for index in range(start, end) if text[index].isalnum()]
        letters = [char for char in word.get("text", "") if char.isalnum()]
        if len(positions) != len(letters):
            return None
        for index, char in zip(positions, letters, strict=True):
            text[index] = char
    return "".join(text)


def _regroup(segments: list[dict], *references: list[dict]) -> tuple[list[dict], dict]:
    """Regroup one stored segment list.

    ``references`` are the segment lists this one was derived from, nearest
    first; their text locates turns when the segment text has been corrected.
    """
    from .worker.pipeline import _rehydrate_segments

    by_signature: dict[tuple, dict] = {}
    for reference in reversed(references):
        by_signature.update({_signature(segment): segment for segment in reference})
    sources = []
    for segment in segments:
        source = by_signature.get(_signature(segment))
        sources.append(_aligned_source(source, segment) if source is not None else None)
    grouped, detail = group_speaker_segments(
        Transcript(segments=_rehydrate_segments(segments), has_speakers=True),
        source_texts=sources,
    )
    return [asdict(segment) for segment in grouped.segments], detail


def _plan(row: PlaudFile, settings: Settings) -> dict:
    """Describe the regrouping of one recording without writing anything."""
    from .worker.pipeline import _select_raw_transcript

    raw = _select_raw_transcript(row, settings)
    if raw is None or raw.source != "local" or not raw.has_speakers:
        return {"status": "no_speaker_transcript"}
    revisions = sorted(
        (rev for rev in row.transcript_revisions if rev.source == "local"),
        key=lambda rev: rev.revision,
    )
    if any(
        rev.kind in _HUMAN_KINDS
        or (rev.kind == "vocabulary" and (rev.note or "").startswith("vocabulary:manual"))
        for rev in revisions
    ):
        return {"status": "skipped_user_edits"}

    diarize = next((run for run in row.stage_runs if run.stage == StageName.diarize), None)
    grouping = ((diarize.detail or {}) if diarize is not None else {}).get("speaker_grouping")
    # Grouping is not strictly idempotent across merged paragraphs; never
    # re-split a transcript this strategy already produced.
    if (grouping or {}).get("turn_strategy") == SPEAKER_TURN_STRATEGY:
        return {"status": "already_current"}

    raw_segments = list(raw.segments or [])
    new_raw, raw_detail = _regroup(raw_segments, raw_segments)
    current = revisions[-1] if revisions else None
    # Regroup the canonical revision and, under an automatic correction, the
    # acoustic base a later re-correction would start from.
    chain: list[TranscriptRevision] = []
    if current is not None and current.base_transcript_id == raw.id:
        if current.kind in _POLISH_KINDS:
            base = next(
                (
                    rev
                    for rev in reversed(revisions[:-1])
                    if rev.base_transcript_id == raw.id and rev.kind in _ACOUSTIC_KINDS
                ),
                None,
            )
            chain = [base, current] if base is not None else [current]
        elif current.kind in _ACOUSTIC_KINDS:
            chain = [current]
    elif current is not None:
        return {"status": "skipped_detached_revision"}

    regrouped = []
    unsafe = raw_detail["unsafe_mixed_segments"]
    changed = new_raw != raw_segments
    previous: list[dict] = raw_segments
    for revision in chain:
        segments, detail = _regroup(list(revision.segments or []), previous, raw_segments)
        unsafe = max(unsafe, detail["unsafe_mixed_segments"])
        changed = changed or segments != revision.segments
        # Once anything beneath changes, every later revision is re-appended so
        # the canonical correction stays on top and matches the new raw lane.
        if changed:
            regrouped.append((revision, segments, detail))
        previous = list(revision.segments or [])
    if current is None:
        displayed_before, displayed_after = raw_segments, new_raw
    else:
        displayed_before = list(current.segments or [])
        displayed_after = next(
            (segments for revision, segments, _ in regrouped if revision is current),
            displayed_before,
        )
    return {
        "status": "changed" if changed else "no_change",
        "raw": raw,
        "new_raw": new_raw,
        "raw_detail": raw_detail,
        "regrouped": regrouped,
        "segments_before": len(displayed_before),
        "segments_after": len(displayed_after),
        "reattributed_seconds": round(_reattributed_seconds(displayed_before, displayed_after), 2),
        "unsafe_mixed_segments": unsafe,
    }


def _summary(file_id: str, plan: dict) -> dict:
    return {"file_id": file_id} | {
        key: value
        for key, value in plan.items()
        if key in {"status", "segments_before", "segments_after", "reattributed_seconds",
                   "unsafe_mixed_segments"}
    }


def scan(settings: Settings | None = None, file_ids: list[str] | None = None) -> list[dict]:
    settings = settings or get_settings()
    results = []
    with session_scope() as session:
        query = select(PlaudFile).where(PlaudFile.is_trash.is_(False))
        if file_ids:
            query = query.where(PlaudFile.id.in_(file_ids))
        for row in session.scalars(query):
            plan = _plan(row, settings)
            if plan["status"] != "no_speaker_transcript":
                results.append(_summary(row.id, plan))
    return results


def apply_regroup(file_id: str, settings: Settings | None = None) -> dict:
    """Atomically regroup one recording's raw and machine-corrected transcript."""
    from .providers.usage import lock_cost_budget
    from .store.speakers import speaker_keys_from_segments, sync_speakers
    from .vocabulary import _mark_derived_stale
    from .worker.knowledge_index import reject_active_ask_evidence_mutation
    from .worker.pipeline import processing_claim_active, raw_transcript_fingerprint

    settings = settings or get_settings()
    with session_scope() as session:
        lock_cost_budget(session, file_id)
        reject_active_ask_evidence_mutation(session, file_id)
        row = session.get(PlaudFile, file_id, populate_existing=True)
        if row is None:
            return {"file_id": file_id, "status": "missing"}
        if processing_claim_active(row) or row.status == FileStatus.processing:
            return {"file_id": file_id, "status": "busy"}
        plan = _plan(row, settings)
        if plan["status"] != "changed":
            return _summary(file_id, plan)

        raw = plan["raw"]
        raw.segments = plan["new_raw"]
        session.flush()
        fingerprint = raw_transcript_fingerprint(raw)
        next_revision = max((rev.revision for rev in row.transcript_revisions), default=0) + 1
        replaced: dict[int, int] = {}
        for revision, segments, detail in plan["regrouped"]:
            snapshot = dict(revision.resolved_profile_snapshot or {})
            if revision.kind in _POLISH_KINDS:
                correction = dict(snapshot.get("correction_input") or {})
                if correction.get("revision") in replaced:
                    correction["revision"] = replaced[correction["revision"]]
                # The wording is the accepted correction of this same raw ASR,
                # now split at new turns: keep it reusable, not re-billed.
                snapshot["correction_input"] = correction | {"raw_sha256": fingerprint}
            snapshot["speaker_regroup"] = {
                "from_revision": revision.revision,
                "turn_strategy": SPEAKER_TURN_STRATEGY,
                "unsafe_mixed_segments": detail["unsafe_mixed_segments"],
            }
            text = "\n".join(
                segment["text"].strip() for segment in segments if segment["text"].strip()
            )
            session.add(
                TranscriptRevision(
                    file_id=file_id,
                    base_transcript_id=raw.id,
                    revision=next_revision,
                    source="local",
                    segments=segments,
                    text=text,
                    has_speakers=all(segment.get("speaker") for segment in segments),
                    note=f"Speaker turns regrouped from revision {revision.revision}",
                    kind=revision.kind,
                    provider=revision.provider,
                    model=revision.model,
                    prompt_version=revision.prompt_version,
                    resolved_profile_snapshot=snapshot,
                )
            )
            replaced[revision.revision] = next_revision
            next_revision += 1
        sync_speakers(session, file_id, speaker_keys_from_segments(plan["new_raw"]))
        diarize = session.scalar(
            select(StageRun).where(StageRun.file_id == file_id, StageRun.stage == StageName.diarize)
        )
        if diarize is not None:
            diarize.detail = dict(diarize.detail or {}) | {
                "speaker_grouping": plan["raw_detail"],
                "speaker_regrouped_at": datetime.now(UTC).isoformat(),
            }
        _mark_derived_stale(session, file_id, reason="speaker_regroup")
        return _summary(file_id, plan) | {"status": "applied"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "apply"):
        command = commands.add_parser(name)
        command.add_argument("file_ids", nargs="*")
        command.add_argument(
            "--min-reattributed-seconds",
            type=float,
            default=0.0,
            help="only include recordings whose displayed speakers change at least this much",
        )
    commands.choices["scan"].add_argument("--json", action="store_true")
    commands.choices["apply"].add_argument(
        "--manifest", type=Path, help="write a notes-regeneration recovery manifest"
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    planned = [
        item
        for item in scan(settings, args.file_ids or None)
        if item["status"] == "changed"
        and item["reattributed_seconds"] >= args.min_reattributed_seconds
    ]
    if args.command == "scan":
        if args.json:
            print(json.dumps(planned, indent=2))
        else:
            for item in sorted(planned, key=lambda i: -i["reattributed_seconds"]):
                print(
                    f"{item['file_id']}  {item['segments_before']:>5} -> "
                    f"{item['segments_after']:<5} reattributed {item['reattributed_seconds']:>8.1f}s"
                    f"  unsafe {item['unsafe_mixed_segments']}"
                )
            print(f"{len(planned)} recordings would change")
        return 0

    applied = []
    for item in planned:
        result = apply_regroup(item["file_id"], settings)
        print(json.dumps(result))
        if result["status"] == "applied":
            applied.append(item["file_id"])
    if args.manifest is not None:
        from .processing_repair import save_manifest

        with session_scope() as session:
            order = {
                row.id: row.start_time_ms or 0
                for row in session.scalars(select(PlaudFile).where(PlaudFile.id.in_(applied)))
            }
        save_manifest(
            args.manifest,
            {
                "recordings": [
                    {"id": file_id, "outcome": "pending"}
                    for file_id in sorted(applied, key=lambda i: -order.get(i, 0))
                ]
            },
        )
    print(f"{len(applied)} recordings regrouped")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through python -m
    raise SystemExit(main())
