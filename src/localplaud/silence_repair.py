"""Operator-only acoustic cleanup for legacy local transcripts.

Planning is read-only with respect to transcript artifacts.  Applying a plan is
an explicitly invoked, fenced mutation which appends one immutable canonical
revision; raw ASR and earlier revisions are never rewritten.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import tempfile
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from typing import Any

from sqlalchemy import select

from .asr.vad import detect_speech
from .config import Settings, VadConfig, get_settings
from .db.models import (
    FileStatus,
    PlaudFile,
    StageName,
    StageRun,
    Transcript,
    TranscriptRevision,
)
from .db.session import session_scope

PLAN_SCHEMA = "localplaud.speech-cleanup-plan/v1"
PROMPT_VERSION = "speech-cleanup/v1"
RETRANSCRIBE_PROMPT_VERSION = "speech-retranscribe/v1"
VAD_CONFIG: dict[str, Any] = {
    "detector": "silero-vad",
    "threshold": 0.25,
    "min_speech_ms": 100,
    "min_silence_ms": 100,
    "speech_pad_ms": 300,
    "classification_expansion_seconds": 1.0,
}
_HUMAN_REVISION_KINDS = frozenset({"user_edit", "restore", "speaker_edit"})
_PLAN_KEYS = frozenset(
    {
        "schema",
        "file_id",
        "raw_transcript_id",
        "canonical_revision",
        "canonical_revision_hash",
        "segments_hash",
        "audio_sha256",
        "audio_role",
        "vad_config",
        "vad_regions",
        "removed_segment_indices",
        "segment_count",
        "generated_title_before",
        "created_at",
    }
)


class SilenceRepairError(RuntimeError):
    """The requested repair cannot safely be planned or applied."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _title_snapshot(row: PlaudFile) -> dict[str, Any]:
    return {
        "title": row.generated_title,
        "provider": row.generated_title_provider,
        "model": row.generated_title_model,
        "created_at": _iso(row.generated_title_at),
    }


def _canonical_snapshot(row: PlaudFile) -> tuple[Transcript, TranscriptRevision | None, list]:
    raw = row.local_transcript
    if raw is None:
        raise SilenceRepairError("recording has no local raw transcript")
    revision = row.corrected_transcript_for_source("local")
    segments = revision.segments if revision is not None else raw.segments
    return raw, revision, copy.deepcopy(segments or [])


def _canonical_hash(raw: Transcript, revision: TranscriptRevision | None, segments: list) -> str:
    return _sha256_json(
        {
            "raw_transcript_id": raw.id,
            "revision": revision.revision if revision is not None else 0,
            "revision_id": revision.id if revision is not None else None,
            "source": revision.source if revision is not None else raw.source,
            "text": revision.text if revision is not None else raw.text,
            "segments": segments,
            "has_speakers": (revision.has_speakers if revision is not None else raw.has_speakers),
        }
    )


def _existing_paths(file_id: str) -> tuple[Path | None, Path | None]:
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise LookupError("recording not found")
        audio = Path(row.audio_path) if row.audio_path else None
        wav = Path(row.wav_path) if row.wav_path else None
    return (
        audio if audio is not None and audio.is_file() else None,
        wav if wav is not None and wav.is_file() else None,
    )


def _ensure_audio(file_id: str, settings: Settings) -> tuple[Path, str, Path | None]:
    """Return the immutable hash basis, its role, and an existing VAD WAV."""
    audio, wav = _existing_paths(file_id)
    if audio is None and wav is None:
        from .imports import ensure_plaud_audio

        audio = Path(ensure_plaud_audio(file_id, settings))
        if not audio.is_file():
            raise SilenceRepairError("recording audio is unavailable")
        _audio, refreshed_wav = _existing_paths(file_id)
        wav = refreshed_wav
    # Hash the exact persistent file inspected by VAD whenever a canonical WAV
    # exists.  This fences a replaced/stale conversion as well as the original.
    if wav is not None:
        return wav, "wav", wav
    if audio is not None:
        return audio, "original", None
    raise SilenceRepairError("recording audio is unavailable")


def _audio_path_for_role(file_id: str, role: str, settings: Settings) -> Path:
    audio, wav = _existing_paths(file_id)
    if role == "original":
        if audio is None:
            from .imports import ensure_plaud_audio

            audio = Path(ensure_plaud_audio(file_id, settings))
        if not audio.is_file():
            raise SilenceRepairError("planned original audio is unavailable")
        return audio
    if role == "wav" and wav is not None:
        return wav
    raise SilenceRepairError("planned WAV audio is unavailable")


def _locked_audio_path(row: PlaudFile, role: str) -> Path | None:
    value = row.audio_path if role == "original" else row.wav_path
    path = Path(value) if value else None
    return path if path is not None and path.is_file() else None


def _normalise_regions(regions: list[tuple[float, float]]) -> list[list[float]]:
    result: list[list[float]] = []
    for start, end in regions:
        try:
            start_f, end_f = float(start), float(end)
        except (TypeError, ValueError):
            continue
        if math.isfinite(start_f) and math.isfinite(end_f) and start_f >= 0 and end_f > start_f:
            result.append([start_f, end_f])
    return sorted(result)


def _valid_segment_range(segment: Any) -> tuple[float, float] | None:
    if not isinstance(segment, dict):
        return None
    start, end = segment.get("start"), segment.get("end")
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    try:
        start_f, end_f = float(start), float(end)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(start_f) and math.isfinite(end_f)):
        return None
    if start_f < 0 or end_f <= start_f:
        return None
    return start_f, end_f


def _removed_indices(segments: list, regions: list[list[float]]) -> list[int]:
    expansion = float(VAD_CONFIG["classification_expansion_seconds"])
    expanded = [(max(0.0, start - expansion), end + expansion) for start, end in regions]
    removed: list[int] = []
    for index, segment in enumerate(segments):
        span = _valid_segment_range(segment)
        if span is None:
            continue
        start, end = span
        if not any(
            max(start, speech_start) < min(end, speech_end) for speech_start, speech_end in expanded
        ):
            removed.append(index)
    return removed


def _vad_regions(audio: Path, existing_wav: Path | None, settings: Settings) -> list[list[float]]:
    cfg = VadConfig(
        enabled=True,
        threshold=VAD_CONFIG["threshold"],
        min_speech_ms=VAD_CONFIG["min_speech_ms"],
        min_silence_ms=VAD_CONFIG["min_silence_ms"],
        speech_pad_ms=VAD_CONFIG["speech_pad_ms"],
    )
    if existing_wav is not None:
        return _normalise_regions(detect_speech(existing_wav, cfg))
    if audio.suffix.casefold() == ".wav":
        return _normalise_regions(detect_speech(audio, cfg))
    from .worker.convert import to_wav

    with tempfile.TemporaryDirectory(prefix="localplaud-speech-cleanup-") as directory:
        wav = to_wav(audio, Path(directory) / "audit.wav")
        return _normalise_regions(detect_speech(wav, cfg))


def plan_repair(file_id: str, settings: Settings | None = None) -> dict[str, Any]:
    """Build a serializable acoustic repair plan without editing transcript data."""
    settings = settings or get_settings()
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise LookupError("recording not found")
        raw, revision, segments = _canonical_snapshot(row)
        raw_id = raw.id
        revision_number = revision.revision if revision is not None else 0
        canonical_hash = _canonical_hash(raw, revision, segments)
        segments_hash = _sha256_json(segments)
        title_before = _title_snapshot(row)

    audio, audio_role, existing_wav = _ensure_audio(file_id, settings)
    audio_sha256 = _sha256_file(audio)
    regions = _vad_regions(audio, existing_wav, settings)
    removed = _removed_indices(segments, regions)
    return {
        "schema": PLAN_SCHEMA,
        "file_id": file_id,
        "raw_transcript_id": raw_id,
        "canonical_revision": revision_number,
        "canonical_revision_hash": canonical_hash,
        "segments_hash": segments_hash,
        "audio_sha256": audio_sha256,
        "audio_role": audio_role,
        "vad_config": dict(VAD_CONFIG),
        "vad_regions": regions,
        "removed_segment_indices": removed,
        "segment_count": len(segments),
        "generated_title_before": title_before,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _validate_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict) or frozenset(plan) != _PLAN_KEYS:
        raise ValueError("invalid speech cleanup plan shape")
    if plan.get("schema") != PLAN_SCHEMA or plan.get("vad_config") != VAD_CONFIG:
        raise ValueError("unsupported speech cleanup plan schema or VAD config")
    if not isinstance(plan.get("file_id"), str) or not plan["file_id"]:
        raise ValueError("invalid plan file_id")
    for key in ("raw_transcript_id", "canonical_revision", "segment_count"):
        if isinstance(plan.get(key), bool) or not isinstance(plan.get(key), int) or plan[key] < 0:
            raise ValueError(f"invalid plan {key}")
    for key in ("canonical_revision_hash", "segments_hash", "audio_sha256"):
        value = plan.get(key)
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(f"invalid plan {key}")
    if plan.get("audio_role") not in {"original", "wav"}:
        raise ValueError("invalid plan audio_role")
    if not isinstance(plan.get("generated_title_before"), dict):
        raise ValueError("invalid generated title snapshot")
    try:
        datetime.fromisoformat(plan["created_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid plan creation time") from exc
    regions = plan.get("vad_regions")
    if not isinstance(regions, list) or _normalise_regions(regions) != regions:
        raise ValueError("invalid plan VAD regions")
    removed = plan.get("removed_segment_indices")
    if (
        not isinstance(removed, list)
        or any(isinstance(index, bool) or not isinstance(index, int) for index in removed)
        or removed != sorted(set(removed))
        or any(index < 0 or index >= plan["segment_count"] for index in removed)
    ):
        raise ValueError("invalid removed segment indices")
    return plan


def _result(
    file_id: str,
    status: str,
    *,
    before: int,
    removed: int = 0,
    revision: int | None = None,
) -> dict[str, Any]:
    return {
        "file_id": file_id,
        "status": status,
        "segments_before": before,
        "segments_removed": removed,
        "segments_after": before - removed,
        "revision": revision,
    }


def _audit_note(*, plan: dict[str, Any], before: int, removed: list[int]) -> str:
    title = plan["generated_title_before"]
    note = {
        "segments_before": before,
        "speech_regions": len(plan["vad_regions"]),
        "removed_indices": removed,
        "previous_generated_title": {
            "sha256": (
                hashlib.sha256(str(title.get("title")).encode("utf-8")).hexdigest()
                if title.get("title") is not None
                else None
            ),
            "provider": title.get("provider"),
            "model": title.get("model"),
            "created_at": title.get("created_at"),
        },
    }
    encoded = json.dumps(note, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) <= 256:
        return encoded
    # Keep the bounded database field useful even for very fragmented recordings.
    compact = {
        "segments_before": before,
        "speech_regions": len(plan["vad_regions"]),
        "removed_count": len(removed),
        "removed_indices_sha256": _sha256_json(removed),
        "previous_generated_title_sha256": _sha256_json(title),
    }
    return json.dumps(compact, sort_keys=True, separators=(",", ":"))


def _prepare_plan_audio(plan: dict[str, Any], settings: Settings) -> None:
    # Restore an evicted original before taking SQLite's write lock.  The hash is
    # repeated while that lock is held below.
    if plan["audio_role"] == "original":
        _audio_path_for_role(plan["file_id"], plan["audio_role"], settings)


def _guard_plan_application(session, plan: dict[str, Any]):
    """Lock and re-check the complete plan snapshot for either apply path."""
    file_id = plan["file_id"]

    from .providers.usage import lock_cost_budget
    from .worker.knowledge_index import reject_active_ask_evidence_mutation
    from .worker.pipeline import processing_claim_active

    lock_cost_budget(session, file_id)
    reject_active_ask_evidence_mutation(session, file_id)
    row = session.get(PlaudFile, file_id, populate_existing=True)
    if row is None:
        raise LookupError("recording not found")
    if processing_claim_active(row) or row.status == FileStatus.processing:
        raise SilenceRepairError("recording is currently processing")

    raw, revision, segments = _canonical_snapshot(row)
    revision_number = revision.revision if revision is not None else 0
    human_revision = session.scalar(
        select(TranscriptRevision.id)
        .where(
            TranscriptRevision.file_id == file_id,
            TranscriptRevision.kind.in_(_HUMAN_REVISION_KINDS)
            | ((TranscriptRevision.kind == "vocabulary")
               & TranscriptRevision.note.like("vocabulary:manual%")),
        )
        .limit(1)
    )
    if human_revision is not None:
        return None, _result(
            file_id, "skipped_user_edits", before=len(segments), revision=revision_number
        )

    stale = (
        raw.id != plan["raw_transcript_id"]
        or revision_number != plan["canonical_revision"]
        or _canonical_hash(raw, revision, segments) != plan["canonical_revision_hash"]
        or _sha256_json(segments) != plan["segments_hash"]
        or len(segments) != plan["segment_count"]
        or _title_snapshot(row) != plan["generated_title_before"]
    )
    if stale:
        return None, _result(file_id, "stale", before=len(segments), revision=revision_number)

    audio_path = _locked_audio_path(row, plan["audio_role"])
    if audio_path is None or _sha256_file(audio_path) != plan["audio_sha256"]:
        return None, _result(file_id, "stale", before=len(segments), revision=revision_number)
    return (row, raw, revision, segments, revision_number), None


def _invalidate_after_transcript_change(
    session, row: PlaudFile, *, reason: str, clear_generated_title: bool
) -> None:
    from .vocabulary import _mark_derived_stale

    if clear_generated_title:
        row.generated_title = None
        row.generated_title_provider = None
        row.generated_title_model = None
        row.generated_title_at = None
    _mark_derived_stale(session, row.id, reason=reason)
    session.flush()
    index_run = session.scalar(
        select(StageRun).where(StageRun.file_id == row.id, StageRun.stage == StageName.index)
    )
    assert index_run is not None
    index_run.detail = dict(index_run.detail or {}) | {
        "reindex_only": True,
        "reason": "canonical transcript changed",
    }


def apply_repair(plan: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """Atomically append the planned cleanup if every snapshot fence still matches."""
    plan = _validate_plan(plan)
    settings = settings or get_settings()
    file_id = plan["file_id"]
    _prepare_plan_audio(plan, settings)

    with session_scope() as session:
        guarded, result = _guard_plan_application(session, plan)
        if result is not None:
            return result
        assert guarded is not None
        row, raw, revision, segments, revision_number = guarded

        removed = list(plan["removed_segment_indices"])
        if removed != _removed_indices(segments, plan["vad_regions"]):
            raise ValueError("plan removal geometry does not match its acoustic evidence")
        if not removed:
            return _result(file_id, "no_change", before=len(segments), revision=revision_number)

        removed_set = set(removed)
        retained = [
            copy.deepcopy(item) for index, item in enumerate(segments) if index not in removed_set
        ]
        new_text = "\n".join(str(segment.get("text") or "") for segment in retained)
        old_text = revision.text if revision is not None else raw.text
        next_revision = max((item.revision for item in row.transcript_revisions), default=0) + 1
        created = TranscriptRevision(
            file_id=file_id,
            base_transcript_id=raw.id,
            revision=next_revision,
            source="local",
            segments=retained,
            text=new_text,
            has_speakers=(revision.has_speakers if revision is not None else raw.has_speakers),
            note=_audit_note(plan=plan, before=len(segments), removed=removed),
            kind="speech_cleanup",
            provider="silero-vad",
            prompt_version=PROMPT_VERSION,
        )
        session.add(created)
        _invalidate_after_transcript_change(
            session,
            row,
            reason="speech_cleanup",
            clear_generated_title=new_text != old_text,
        )
        return _result(
            file_id,
            "applied",
            before=len(segments),
            removed=len(removed),
            revision=next_revision,
        )


def _finite_timestamp(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a finite nonnegative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a finite nonnegative number")
    return number


def _validate_retranscription(transcript: Any) -> tuple[list[dict], bool]:
    if not isinstance(transcript, dict):
        raise ValueError("retranscription must be an object")
    segments = transcript.get("segments")
    has_speakers = transcript.get("has_speakers")
    if not isinstance(segments, list):
        raise ValueError("retranscription segments must be a list")
    if not isinstance(has_speakers, bool):
        raise ValueError("retranscription has_speakers must be a boolean")

    validated = copy.deepcopy(segments)
    previous_start = -1.0
    for segment_index, segment in enumerate(validated):
        if not isinstance(segment, dict):
            raise ValueError(f"segment {segment_index} must be an object")
        if not isinstance(segment.get("text"), str):
            raise ValueError(f"segment {segment_index} text must be a string")
        start = _finite_timestamp(segment.get("start"), f"segment {segment_index} start")
        end = _finite_timestamp(segment.get("end"), f"segment {segment_index} end")
        if end <= start:
            raise ValueError(f"segment {segment_index} end must be after start")
        if start < previous_start:
            raise ValueError("retranscription segments must be ordered by start time")
        previous_start = start

        if "words" not in segment:
            continue
        words = segment["words"]
        if not isinstance(words, list):
            raise ValueError(f"segment {segment_index} words must be a list")
        previous_word_start = start
        for word_index, word in enumerate(words):
            if not isinstance(word, dict):
                raise ValueError(f"segment {segment_index} word {word_index} must be an object")
            if not isinstance(word.get("text"), str):
                raise ValueError(f"segment {segment_index} word {word_index} text must be a string")
            word_start = _finite_timestamp(
                word.get("start"), f"segment {segment_index} word {word_index} start"
            )
            word_end = _finite_timestamp(
                word.get("end"), f"segment {segment_index} word {word_index} end"
            )
            # Whisper quantizes word boundaries; punctuation and short words
            # can legitimately have zero duration. Preserve that evidence.
            if word_end < word_start:
                raise ValueError(
                    f"segment {segment_index} word {word_index} end must not precede start"
                )
            if word_start < start - 1e-6 or word_end > end + 1e-6:
                raise ValueError(
                    f"segment {segment_index} word {word_index} timestamps exceed segment bounds"
                )
            if word_start < previous_word_start:
                raise ValueError(f"segment {segment_index} words must be ordered by start time")
            previous_word_start = word_start
    return validated, has_speakers


def _validate_profile_snapshot(profile_snapshot: Any) -> dict[str, Any]:
    if not isinstance(profile_snapshot, dict):
        raise ValueError("profile_snapshot must be a resolved profile object")
    try:
        json.dumps(profile_snapshot, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("profile_snapshot must be JSON-serializable") from exc
    if profile_snapshot.get("schema") != "localplaud-resolved-profile/v2":
        raise ValueError("profile_snapshot must use localplaud-resolved-profile/v2")
    if not isinstance(profile_snapshot.get("policy"), dict):
        raise ValueError("profile_snapshot policy must be an object")
    if not isinstance(profile_snapshot.get("layers"), list) or not isinstance(
        profile_snapshot.get("layer_provenance"), list
    ):
        raise ValueError("profile_snapshot must include resolved layer provenance")
    stages = profile_snapshot.get("stages")
    if not isinstance(stages, dict):
        raise ValueError("profile_snapshot stages must be an object")
    for stage in ("transcribe", "diarize"):
        selection = stages.get(stage)
        if not isinstance(selection, dict):
            raise ValueError(f"profile_snapshot must include resolved {stage} provenance")
        if not all(
            isinstance(selection.get(field), str) and selection[field].strip()
            for field in ("connection", "model")
        ):
            raise ValueError(f"profile_snapshot {stage} requires connection and model")
    from .providers.service import _validate_connection_config

    _validate_connection_config(profile_snapshot, "profile_snapshot")
    return copy.deepcopy(profile_snapshot)


def _retranscription_result(
    file_id: str,
    status: str,
    *,
    before: int,
    after: int,
    changed: int,
    revision: int | None,
) -> dict[str, Any]:
    return {
        "file_id": file_id,
        "status": status,
        "segments_before": before,
        "segments_after": after,
        "segments_changed": changed,
        "revision": revision,
    }


def _retranscription_note(plan: dict[str, Any], *, before: int, after: int, changed: int) -> str:
    title = plan["generated_title_before"]
    note = {
        "method": "full_retranscription",
        "plan": {
            "schema": plan["schema"],
            "sha256": _sha256_json(plan),
            "canonical_revision": plan["canonical_revision"],
            "audio_sha256": plan["audio_sha256"],
        },
        "segments_before": before,
        "segments_after": after,
        "segments_changed": changed,
        "previous_generated_title": {
            "sha256": _sha256_json(title),
            "provider": title.get("provider"),
            "model": title.get("model"),
            "created_at": title.get("created_at"),
        },
    }
    encoded = json.dumps(note, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded) <= 256:
        return encoded
    compact = {
        "method": "full_retranscription",
        "plan": _sha256_json(plan),
        "segments_before": before,
        "segments_after": after,
        "segments_changed": changed,
        "old_title": _sha256_json(title),
    }
    return json.dumps(compact, sort_keys=True, separators=(",", ":"))


def apply_retranscription(
    plan: dict[str, Any],
    transcript: dict,
    *,
    provider: str,
    model: str,
    profile_snapshot: dict,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Append a reviewed ASR+diarization replacement behind the plan fences."""
    plan = _validate_plan(plan)
    replacement, has_speakers = _validate_retranscription(transcript)
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a nonempty string")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a nonempty string")
    resolved_snapshot = _validate_profile_snapshot(profile_snapshot)
    settings = settings or get_settings()
    file_id = plan["file_id"]
    _prepare_plan_audio(plan, settings)

    with session_scope() as session:
        guarded, result = _guard_plan_application(session, plan)
        if result is not None:
            return _retranscription_result(
                file_id,
                result["status"],
                before=result["segments_before"],
                after=result["segments_before"],
                changed=0,
                revision=result["revision"],
            )
        assert guarded is not None
        row, raw, revision, segments, revision_number = guarded
        # Re-diarization may assign different cluster labels. Do not attach a
        # human-assigned name to an unreviewed new voice by reusing that label.
        if any(speaker.display_name for speaker in row.speakers):
            return _retranscription_result(
                file_id, "skipped_user_edits", before=len(segments),
                after=len(segments), changed=0, revision=revision_number,
            )
        new_text = "\n".join(segment["text"] for segment in replacement)
        old_text = revision.text if revision is not None else raw.text
        current_has_speakers = revision.has_speakers if revision is not None else raw.has_speakers
        if (replacement == segments and new_text == old_text
                and has_speakers == current_has_speakers):
            return _retranscription_result(
                file_id,
                "no_change",
                before=len(segments),
                after=len(replacement),
                changed=0,
                revision=revision_number,
            )

        changed = sum(
            before_segment != after_segment
            for before_segment, after_segment in zip_longest(segments, replacement)
        )
        next_revision = max((item.revision for item in row.transcript_revisions), default=0) + 1
        session.add(
            TranscriptRevision(
                file_id=file_id,
                base_transcript_id=raw.id,
                revision=next_revision,
                source="local",
                segments=replacement,
                text=new_text,
                has_speakers=has_speakers,
                note=_retranscription_note(
                    plan,
                    before=len(segments),
                    after=len(replacement),
                    changed=changed,
                ),
                kind="speech_retranscribe",
                provider=provider,
                model=model,
                prompt_version=RETRANSCRIBE_PROMPT_VERSION,
                resolved_profile_snapshot=resolved_snapshot,
            )
        )
        _invalidate_after_transcript_change(
            session,
            row,
            reason="speech_retranscribe",
            clear_generated_title=True,
        )
        return _retranscription_result(
            file_id,
            "applied",
            before=len(segments),
            after=len(replacement),
            changed=changed,
            revision=next_revision,
        )


def _load_plan(path: Path) -> dict[str, Any]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError("plan JSONL must contain exactly one object")
    return json.loads(lines[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operator-only acoustic transcript repair")
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("file_id")
    plan_parser.add_argument("--output", type=Path)
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("plan", type=Path)
    args = parser.parse_args(argv)

    if args.command == "plan":
        plan = plan_repair(args.file_id)
        encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if args.output is not None:
            args.output.write_text(encoded + "\n", encoding="utf-8")
            print(
                json.dumps(
                    {
                        "file_id": args.file_id,
                        "status": "planned",
                        "removed_segments": len(plan["removed_segment_indices"]),
                    }
                )
            )
        else:
            print(encoded)
        return 0

    result = apply_repair(_load_plan(args.plan))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through python -m
    raise SystemExit(main())
