"""Auditable subscription-independence checks for one recording."""

from __future__ import annotations

from pathlib import Path

from .db.models import PlaudFile, StageStatus
from .db.session import session_scope
from .error_redaction import sanitize_error
from .export_formats import render_notes, render_transcript, transcript_provenance


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def subscription_independence_report(file_id: str) -> dict:
    """Return concrete evidence that one recording is usable without Plaud AI."""
    with session_scope() as session:
        file = session.get(PlaudFile, file_id)
        if file is None:
            raise LookupError(f"recording not found: {file_id}")
        transcript = file.local_transcript
        polished = next(
            (
                row
                for row in reversed(file.transcript_revisions)
                if row.kind == "ai_polish" and row.source == "local"
            ),
            None,
        )
        segments = list((transcript.segments if transcript else []) or [])
        word_count = sum(len(item.get("words") or []) for item in segments)
        local_notes = [
            row for row in file.summaries if row.source == "local" and row.template != "mind_map"
        ]
        mind_maps = [
            row for row in file.summaries if row.source == "local" and row.template == "mind_map"
        ]
        chunks = list(file.chunks)
        speakers = list(file.speakers)
        stage_runs = list(file.stage_runs)
        correct_stage = next(
            (row for row in stage_runs if row.stage.value == "correct"), None
        )
        align_stage = next(
            (row for row in stage_runs if row.stage.value == "align"), None
        )
        diarize_stage = next(
            (row for row in stage_runs if row.stage.value == "diarize"), None
        )
        expected_transcript_id = transcript.id if transcript is not None else None
        expected_revision = polished.revision if polished is not None else None
        checks = [
            _check(
                "raw_audio_local",
                bool(file.audio_path and Path(file.audio_path).is_file()),
                "original/cached audio exists on this host",
            ),
            _check(
                "local_transcript",
                transcript is not None and transcript.source == "local",
                "canonical transcript has source=local",
            ),
            _check(
                "transcript_polish",
                polished is not None
                and transcript is not None
                and polished.base_transcript_id == transcript.id
                and bool(polished.provider and polished.model and polished.prompt_version)
                and correct_stage is not None
                and correct_stage.status == StageStatus.completed,
                (
                    f"AI polish revision {polished.revision} via "
                    f"{polished.provider}:{polished.model} · {polished.prompt_version}"
                    if polished is not None
                    else "no local AI-polished transcript revision"
                ),
            ),
            _check(
                "timestamped_segments",
                bool(segments)
                and all(item.get("start") is not None and item.get("end") is not None for item in segments),
                f"{len(segments)} segment(s) with start/end timestamps",
            ),
            _check(
                "word_alignment",
                word_count > 0
                and align_stage is not None
                and align_stage.status == StageStatus.completed
                and align_stage.detail.get("forced_alignment") is True,
                (
                    f"{word_count} word timestamp(s) forced-aligned by "
                    f"{align_stage.provider}:{align_stage.model}"
                    if word_count and align_stage and align_stage.detail.get("forced_alignment") is True
                    else "no completed forced-alignment evidence"
                ),
            ),
            _check(
                "speaker_assignment",
                bool(segments)
                and all(item.get("speaker") for item in segments)
                and bool(speakers),
                f"{len(speakers)} stable speaker identity row(s)",
            ),
            _check(
                "speaker_diarization",
                diarize_stage is not None
                and diarize_stage.status == StageStatus.completed
                and bool(diarize_stage.provider and diarize_stage.model),
                (
                    f"durable diarization via "
                    f"{diarize_stage.provider}:{diarize_stage.model}"
                    if diarize_stage is not None
                    else "no durable diarization stage evidence"
                ),
            ),
            _check(
                "local_notes",
                bool(local_notes)
                and expected_transcript_id is not None
                and expected_revision is not None
                and all(
                    row.input_transcript_source == "local"
                    and row.input_transcript_id == expected_transcript_id
                    and row.input_transcript_revision == expected_revision
                    for row in local_notes
                ),
                f"{len(local_notes)} local note output(s) from transcript "
                f"{expected_transcript_id} revision {expected_revision}",
            ),
            _check(
                "local_mind_map",
                bool(mind_maps)
                and expected_transcript_id is not None
                and expected_revision is not None
                and all(
                    row.input_transcript_source == "local"
                    and row.input_transcript_id == expected_transcript_id
                    and row.input_transcript_revision == expected_revision
                    for row in mind_maps
                ),
                f"{len(mind_maps)} local mind map(s) from transcript "
                f"{expected_transcript_id} revision {expected_revision}",
            ),
            _check(
                "ask_index",
                bool(chunks)
                and expected_transcript_id is not None
                and expected_revision is not None
                and all(
                    row.input_transcript_source == "local"
                    and row.input_transcript_id == expected_transcript_id
                    and row.input_transcript_revision == expected_revision
                    for row in chunks
                ),
                f"{len(chunks)} grounded chunk(s) from transcript "
                f"{expected_transcript_id} revision {expected_revision}",
            ),
            _check(
                "durable_stages",
                bool(stage_runs)
                and all(row.status != StageStatus.failed for row in stage_runs)
                and all(row.resolved_profile_snapshot for row in stage_runs),
                f"{len(stage_runs)} durable stage record(s) with resolved profiles",
            ),
        ]

    export_errors: list[str] = []
    for fmt in ("txt", "srt", "vtt", "docx", "pdf"):
        try:
            payload, _media_type = render_transcript(file_id, fmt)
            if not payload:
                export_errors.append(f"transcript {fmt}: empty")
        except Exception as exc:  # noqa: BLE001 - report evidence, do not hide it
            export_errors.append(f"transcript {fmt}: {sanitize_error(exc, max_length=500)}")
    for fmt in ("md", "txt", "docx", "pdf"):
        try:
            payload, _media_type = render_notes(file_id, fmt)
            if not payload:
                export_errors.append(f"notes {fmt}: empty")
        except Exception as exc:  # noqa: BLE001 - report evidence, do not hide it
            export_errors.append(f"notes {fmt}: {sanitize_error(exc, max_length=500)}")
    provenance = transcript_provenance(file_id)
    checks.append(
        _check(
            "required_exports",
            not export_errors
            and provenance.get("transcript_source") == "local"
            and provenance.get("transcript_id") == expected_transcript_id
            and provenance.get("transcript_revision") == expected_revision,
            "transcript TXT/SRT/VTT/DOCX/PDF and notes MD/TXT/DOCX/PDF render locally"
            if not export_errors
            else "; ".join(export_errors),
        )
    )
    return {
        "schema": "localplaud-subscription-independence/v1",
        "file_id": file_id,
        "passed": all(item["passed"] for item in checks),
        "checks": checks,
    }
