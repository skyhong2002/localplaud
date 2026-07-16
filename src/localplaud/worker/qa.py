"""Q&A and semantic search over the transcript knowledge base.

localplaud's answer to "Ask Plaud": embed the question, retrieve the closest
chunks by cosine similarity, and let the LLM answer grounded in them, citing
the source recordings.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import quote

import numpy as np
from sqlalchemy import exists, func, or_, select

from ..config import Settings, get_settings
from ..date_filters import normalize_calendar_date, resolve_date_scope
from ..db.models import (
    Chunk,
    KnowledgeChunk,
    KnowledgeDocument,
    PlaudFile,
    Speaker,
    Summary,
    Tag,
    Transcript,
    TranscriptRevision,
    UserNote,
)
from ..db.session import session_scope
from ..embeddings.base import build_embedder
from ..error_redaction import sanitize_error
from ..llm.base import build_llm
from ..providers.fallback import candidate_snapshots, is_retryable_fallback_error
from ..providers.service import preview_resolution, resolve_recording_profile
from ..providers.usage import (
    CostPolicyError,
    estimate_cost,
    finalize_provider_cost_reservations,
    lock_cost_budget,
    normalize_usage,
    pricing_for_stage,
    provider_cost_reservation_total,
    provider_dispatch_fingerprint,
    reserve_provider_cost,
)
from .pipeline import _settings_for_stage

log = logging.getLogger(__name__)

_ask_dispatch_context: ContextVar[dict | None] = ContextVar(
    "localplaud_ask_dispatch_context", default=None
)


@contextmanager
def provider_dispatch_guard(before_dispatch: Callable[[], None]) -> Iterator[dict]:
    """Install one request-local claim guard without changing ``answer``'s API."""
    state: dict = {"before_dispatch": before_dispatch, "evidence_fingerprints": []}
    token = _ask_dispatch_context.set(state)
    try:
        yield state
    finally:
        _ask_dispatch_context.reset(token)


def _before_provider_dispatch() -> None:
    state = _ask_dispatch_context.get()
    if state is not None:
        state["before_dispatch"]()


def _user_note_evidence_fingerprint(note: UserNote) -> str:
    payload = {
        "title": note.title,
        "content_md": note.content_md,
        "version": note.version,
        "source_type": note.source_type,
        "source_summary_snapshot": note.source_summary_snapshot or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _artifact_evidence_fingerprint(artifact: Summary | UserNote) -> str:
    if isinstance(artifact, Summary):
        from ..note_history import fingerprint_digest

        return fingerprint_digest(artifact)
    return _user_note_evidence_fingerprint(artifact)


def _note_evidence_identity(
    document: KnowledgeDocument,
    artifact: Summary | UserNote,
    library_scope: dict | None,
) -> dict:
    return {
        "evidence_type": "note",
        "document_id": document.id,
        "kind": document.kind,
        "file_id": document.file_id,
        "summary_id": document.summary_id,
        "user_note_id": document.user_note_id,
        "artifact_version": document.artifact_version,
        "content_sha256": document.content_sha256,
        "generation": document.generation,
        "artifact_fingerprint": _artifact_evidence_fingerprint(artifact),
        "library_scope": copy.deepcopy(library_scope),
    }


def _transcript_chunk_fingerprint(chunk: Chunk) -> str:
    payload = {
        "id": chunk.id,
        "file_id": chunk.file_id,
        "idx": chunk.idx,
        "text": chunk.text,
        "start": chunk.start,
        "end": chunk.end,
        "speaker": chunk.speaker,
        "embedding_model": chunk.embedding_model,
        "dim": chunk.dim,
        "embedding_sha256": (
            hashlib.sha256(chunk.embedding).hexdigest() if chunk.embedding is not None else None
        ),
        "input_transcript_id": chunk.input_transcript_id,
        "input_transcript_revision": chunk.input_transcript_revision,
        "input_transcript_source": chunk.input_transcript_source,
        "resolved_profile_snapshot": chunk.resolved_profile_snapshot or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _canonical_transcript_fingerprint(session, chunk: Chunk) -> str | None:
    """Hash the exact canonical transcript body behind a current indexed chunk."""
    transcript_id = chunk.input_transcript_id
    revision_number = chunk.input_transcript_revision
    source = chunk.input_transcript_source
    if transcript_id is None and revision_number is None and source is None:
        return None
    if not isinstance(transcript_id, int) or not isinstance(revision_number, int) or not source:
        return "invalid-lineage"
    raw = session.get(Transcript, transcript_id, populate_existing=True)
    if raw is None or raw.file_id != chunk.file_id or raw.source != source:
        return "missing-transcript"
    artifact: Transcript | TranscriptRevision = raw
    if revision_number:
        revision = session.scalar(
            select(TranscriptRevision).where(
                TranscriptRevision.file_id == chunk.file_id,
                TranscriptRevision.source == source,
                TranscriptRevision.revision == revision_number,
            )
        )
        if revision is None:
            return "missing-revision"
        artifact = revision
    payload = {
        "transcript_id": transcript_id,
        "revision": revision_number,
        "source": source,
        "text": artifact.text,
        "segments": artifact.segments or [],
        "has_speakers": artifact.has_speakers,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _speaker_registry_fingerprint(speakers: list[Speaker]) -> str:
    payload = sorted((speaker.key, speaker.display_name) for speaker in speakers)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _transcript_evidence_identity(
    chunk: Chunk,
    library_scope: dict | None,
    speakers: list[Speaker],
    *,
    speaker_key: str | None,
    canonical_transcript_fingerprint: str | None,
) -> dict:
    return {
        "evidence_type": "transcript",
        "chunk_id": chunk.id,
        "file_id": chunk.file_id,
        "speaker_key": speaker_key,
        "input_transcript_id": chunk.input_transcript_id,
        "input_transcript_revision": chunk.input_transcript_revision,
        "input_transcript_source": chunk.input_transcript_source,
        "chunk_fingerprint": _transcript_chunk_fingerprint(chunk),
        "canonical_transcript_fingerprint": canonical_transcript_fingerprint,
        "speaker_registry_fingerprint": _speaker_registry_fingerprint(speakers),
        "library_scope": copy.deepcopy(library_scope),
    }


def _evidence_fingerprints(hits: list[dict]) -> list[dict]:
    unique: dict[tuple[str, int], dict] = {}
    for hit in hits:
        fingerprint = hit.get("_evidence_fingerprint")
        if fingerprint is not None:
            evidence_type = fingerprint.get("evidence_type", "note")
            evidence_id = (
                fingerprint.get("chunk_id")
                if evidence_type == "transcript"
                else fingerprint.get("document_id")
            )
            if evidence_id is not None:
                unique[(evidence_type, int(evidence_id))] = copy.deepcopy(fingerprint)
    return [unique[key] for key in sorted(unique)]


def _public_hits(hits: list[dict]) -> list[dict]:
    public = copy.deepcopy(hits)
    for hit in public:
        hit.pop("_evidence_fingerprint", None)
    return public


def _file_matches_library_scope(
    session, file_id: str, scope: dict, *, speaker_key: str | None
) -> bool:
    """Re-evaluate mutable recording membership at an Ask publication boundary."""
    row = session.get(PlaudFile, file_id, populate_existing=True)
    if row is None or row.is_trash:
        return False
    if scope.get("folder_id") and row.folder_id != scope["folder_id"]:
        return False
    if scope.get("tag_id") and scope["tag_id"] not in {tag.id for tag in row.tags}:
        return False
    if scope.get("origin") == "plaud" and row.origin not in {None, "plaud"}:
        return False
    if scope.get("origin") == "local" and row.origin != "local":
        return False
    if scope.get("date_from_ms") is not None and (
        row.start_time_ms is None or row.start_time_ms < scope["date_from_ms"]
    ):
        return False
    if scope.get("date_to_ms_exclusive") is not None and (
        row.start_time_ms is None or row.start_time_ms >= scope["date_to_ms_exclusive"]
    ):
        return False
    if scope.get("file_ids") and file_id not in scope["file_ids"]:
        return False
    if scope.get("speaker_name"):
        if speaker_key is None:
            return False
        matching_speaker = session.scalar(
            select(Speaker.id).where(
                Speaker.file_id == file_id,
                Speaker.key == speaker_key,
                Speaker.display_name.is_not(None),
                func.lower(Speaker.display_name) == scope["speaker_name"].lower(),
            )
        )
        if matching_speaker is None:
            return False
    return True


def _transcript_lineage_is_current(session, expected: dict, chunk: Chunk) -> bool:
    """Prove an indexed transcript chunk still names the canonical source revision."""
    if _transcript_chunk_fingerprint(chunk) != expected.get("chunk_fingerprint"):
        return False
    transcript_id = expected.get("input_transcript_id")
    revision = expected.get("input_transcript_revision")
    source = expected.get("input_transcript_source")
    # Legacy indexes did not persist lineage. Their exact row fingerprint still
    # fences mutation; current indexes additionally prove canonical lineage.
    if transcript_id is None and revision is None and source is None:
        return True
    if not isinstance(transcript_id, int) or not isinstance(revision, int) or not source:
        return False
    row = session.get(PlaudFile, chunk.file_id, populate_existing=True)
    raw = session.get(Transcript, transcript_id, populate_existing=True)
    if row is None or raw is None or raw.file_id != chunk.file_id or raw.source != source:
        return False
    source_rows = [item for item in row.transcripts if item.source == source]
    if not source_rows or source_rows[-1].id != transcript_id:
        return False
    current_revision = row.corrected_transcript_for_source(source)
    return (
        (current_revision.revision if current_revision is not None else 0) == revision
        and _canonical_transcript_fingerprint(session, chunk)
        == expected.get("canonical_transcript_fingerprint")
    )


def validate_evidence_fingerprints(session, fingerprints: list[dict]) -> None:
    """Prove exact transcript/note evidence and scope at an egress/save boundary."""
    if not fingerprints:
        return
    file_ids = sorted(
        {item["file_id"] for item in fingerprints if item.get("file_id") is not None}
    )
    if any(item.get("file_id") is None for item in fingerprints):
        lock_cost_budget(session, None)
    for evidence_file_id in file_ids:
        lock_cost_budget(session, evidence_file_id)

    transcript_fingerprints = [
        item for item in fingerprints if item.get("evidence_type") == "transcript"
    ]
    note_fingerprints = [
        item for item in fingerprints if item.get("evidence_type", "note") == "note"
    ]
    chunk_ids = sorted({int(item["chunk_id"]) for item in transcript_fingerprints})
    chunks = {
        row.id: row
        for row in session.scalars(
            select(Chunk)
            .where(Chunk.id.in_(chunk_ids))
            .order_by(Chunk.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    for expected in transcript_fingerprints:
        chunk = chunks.get(int(expected["chunk_id"]))
        library_scope = expected.get("library_scope")
        speakers = list(
            session.scalars(
                select(Speaker)
                .where(Speaker.file_id == expected.get("file_id"))
                .order_by(Speaker.id)
            )
        )
        current = (
            chunk is not None
            and chunk.file_id == expected.get("file_id")
            and _transcript_lineage_is_current(session, expected, chunk)
            and _speaker_registry_fingerprint(speakers)
            == expected.get("speaker_registry_fingerprint")
            and (
                library_scope is None
                or _file_matches_library_scope(
                    session,
                    chunk.file_id,
                    normalize_library_scope(library_scope),
                    speaker_key=expected.get("speaker_key"),
                )
            )
        )
        if not current:
            raise RuntimeError("Ask evidence changed before completion; retry the request")

    summary_ids = sorted(
        {item["summary_id"] for item in note_fingerprints if item.get("summary_id") is not None}
    )
    user_note_ids = sorted(
        {
            item["user_note_id"]
            for item in note_fingerprints
            if item.get("user_note_id") is not None
        }
    )
    summaries = {
        row.id: row
        for row in session.scalars(_ordered_artifact_lock_query(Summary, summary_ids))
    }
    user_notes = {
        row.id: row
        for row in session.scalars(_ordered_artifact_lock_query(UserNote, user_note_ids))
    }
    document_ids = sorted({int(item["document_id"]) for item in note_fingerprints})
    documents = {
        row.id: row
        for row in session.scalars(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id.in_(document_ids))
            .order_by(KnowledgeDocument.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    }
    from .knowledge_index import knowledge_document_is_current

    for expected in note_fingerprints:
        document = documents.get(int(expected["document_id"]))
        artifact = (
            summaries.get(expected.get("summary_id"))
            if expected["kind"] == "generated_summary"
            else user_notes.get(expected.get("user_note_id"))
        )
        current = (
            document is not None
            and artifact is not None
            and all(
                getattr(document, field) == expected.get(field)
                for field in (
                    "kind",
                    "file_id",
                    "summary_id",
                    "user_note_id",
                    "artifact_version",
                    "content_sha256",
                    "generation",
                )
            )
            and _artifact_evidence_fingerprint(artifact)
            == expected.get("artifact_fingerprint")
            and knowledge_document_is_current(session, document)
            and (
                expected.get("library_scope") is None
                or (
                    document.file_id is None
                    and not normalize_library_scope(expected["library_scope"])
                )
                or (
                    document.file_id is not None
                    and _file_matches_library_scope(
                        session,
                        document.file_id,
                        normalize_library_scope(expected["library_scope"]),
                        speaker_key=None,
                    )
                )
            )
        )
        if not current:
            raise RuntimeError("Ask evidence changed before completion; retry the request")


def _dispatch_with_current_evidence(
    hits: list[dict], dispatch: Callable[[], str]
) -> str:
    fingerprints = _evidence_fingerprints(hits)
    state = _ask_dispatch_context.get()
    if state is not None:
        state["evidence_fingerprints"] = copy.deepcopy(fingerprints)
    _before_provider_dispatch()
    with session_scope() as session:
        validate_evidence_fingerprints(session, fingerprints)
        # Web Ask holds a durable request lease that note mutations honor, so do
        # not retain SQLite's global writer lock during a potentially long LLM
        # request. Direct callers without that lease keep the conservative legacy
        # behavior until they adopt a durable request scope.
        if state is None:
            return dispatch()
    return dispatch()


def normalize_library_scope(value: dict | None) -> dict:
    """Validate and canonicalize a durable whole-library retrieval boundary."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("library Ask scope must be an object")
    unknown = sorted(
        set(value)
        - {
            "folder_id",
            "tag_id",
            "origin",
            "speaker_name",
            "date_from",
            "date_to",
            "scope_version",
            "date_timezone",
            "date_from_ms",
            "date_to_ms_exclusive",
            "file_ids",
        }
    )
    if unknown:
        raise ValueError(f"unknown library Ask scope fields: {', '.join(unknown)}")
    scope: dict = {}
    for key in ("folder_id", "tag_id"):
        raw = value.get(key)
        if raw not in (None, ""):
            try:
                parsed = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} must be a positive integer") from exc
            if parsed <= 0:
                raise ValueError(f"{key} must be a positive integer")
            scope[key] = parsed
    origin = value.get("origin")
    if origin not in (None, ""):
        if origin not in {"plaud", "local"}:
            raise ValueError("origin must be plaud or local")
        scope["origin"] = origin
    speaker_name = value.get("speaker_name")
    if speaker_name not in (None, ""):
        if not isinstance(speaker_name, str):
            raise ValueError("speaker_name must be text")
        speaker_name = " ".join(speaker_name.split())
        if not speaker_name or len(speaker_name) > 128:
            raise ValueError("speaker_name must contain 1 to 128 characters")
        scope["speaker_name"] = speaker_name
    normalized_dates = {}
    for key in ("date_from", "date_to"):
        raw = value.get(key)
        if raw not in (None, ""):
            try:
                normalized_dates[key] = normalize_calendar_date(raw)
            except ValueError as exc:
                raise ValueError(f"invalid {key}: {exc}") from exc
    date_metadata = {
        "scope_version",
        "date_timezone",
        "date_from_ms",
        "date_to_ms_exclusive",
    }
    if normalized_dates:
        version = value.get("scope_version")
        if version is None:
            resolved_dates = resolve_date_scope(
                normalized_dates.get("date_from"),
                normalized_dates.get("date_to"),
                "UTC",
                scope_version=1,
            )
        else:
            if not isinstance(version, int) or isinstance(version, bool) or version not in {1, 2}:
                raise ValueError("scope_version must be 1 or 2")
            timezone = value.get("date_timezone")
            if version == 1 and timezone != "UTC":
                raise ValueError("legacy date scope timezone must be UTC")
            resolved_dates = resolve_date_scope(
                normalized_dates.get("date_from"),
                normalized_dates.get("date_to"),
                timezone,
                scope_version=version,
            )
            for key in ("date_from_ms", "date_to_ms_exclusive"):
                expected = resolved_dates.get(key)
                raw = value.get(key)
                if expected is None:
                    if raw is not None:
                        raise ValueError(f"{key} has no matching calendar bound")
                    continue
                if not isinstance(raw, int) or isinstance(raw, bool) or raw != expected:
                    raise ValueError(f"{key} does not match the date scope")
        scope.update(resolved_dates)
    elif any(value.get(key) is not None for key in date_metadata):
        raise ValueError("date scope metadata requires date_from or date_to")
    file_ids = value.get("file_ids")
    if file_ids not in (None, []):
        if not isinstance(file_ids, list) or not all(
            isinstance(item, str) and item.strip() for item in file_ids
        ):
            raise ValueError("file_ids must be a list of recording IDs")
        unique = list(dict.fromkeys(item.strip() for item in file_ids))
        if len(unique) > 100:
            raise ValueError("file_ids cannot contain more than 100 recordings")
        scope["file_ids"] = unique
    return scope


def _load_matrix(
    session,
    dim: int,
    file_id: str | None = None,
    retrieval_scope: dict | None = None,
    embedding_snapshot: dict | None = None,
) -> tuple[list[dict], np.ndarray]:
    # Only chunks embedded at the query's dimension are comparable; mixing dims
    # (e.g. after switching embeddings.provider) would crash np.stack / the dot
    # product. Filter to the current embedder's dimension. When ``file_id`` is
    # set, scope retrieval to a single recording (single-file Ask).
    stmt = (
        select(Chunk)
        .join(PlaudFile, PlaudFile.id == Chunk.file_id)
        .where(Chunk.embedding.is_not(None), Chunk.dim == dim)
    )
    if file_id is not None:
        stmt = stmt.where(Chunk.file_id == file_id)
    else:
        stmt = stmt.where(PlaudFile.is_trash.is_(False))
    scope = normalize_library_scope(retrieval_scope)
    if file_id is not None and scope:
        raise ValueError("single-recording Ask cannot use a library scope")
    if scope:
        if scope.get("folder_id"):
            stmt = stmt.where(PlaudFile.folder_id == scope["folder_id"])
        if scope.get("tag_id"):
            stmt = stmt.where(PlaudFile.tags.any(Tag.id == scope["tag_id"]))
        if scope.get("origin") == "plaud":
            stmt = stmt.where(or_(PlaudFile.origin == "plaud", PlaudFile.origin.is_(None)))
        elif scope.get("origin") == "local":
            stmt = stmt.where(PlaudFile.origin == scope["origin"])
        if scope.get("speaker_name"):
            stmt = stmt.where(
                exists(
                    select(Speaker.id).where(
                        Speaker.file_id == Chunk.file_id,
                        Speaker.key == Chunk.speaker,
                        Speaker.display_name.is_not(None),
                        func.lower(Speaker.display_name) == scope["speaker_name"].lower(),
                    )
                )
            )
        if scope.get("date_from_ms") is not None:
            stmt = stmt.where(PlaudFile.start_time_ms >= scope["date_from_ms"])
        if scope.get("date_to_ms_exclusive") is not None:
            stmt = stmt.where(PlaudFile.start_time_ms < scope["date_to_ms_exclusive"])
        if scope.get("file_ids"):
            stmt = stmt.where(PlaudFile.id.in_(scope["file_ids"]))
    from .knowledge_index import embedding_identity

    query_identity = embedding_identity(embedding_snapshot) if embedding_snapshot else None
    transcript_chunks = [
        chunk
        for chunk in session.scalars(stmt)
        if query_identity is None
        or (
            chunk.resolved_profile_snapshot is not None
            and embedding_identity(chunk.resolved_profile_snapshot) == query_identity
        )
    ]
    candidates = [
        {"kind": "transcript", "chunk": chunk, "document": None} for chunk in transcript_chunks
    ]

    # Notes use a separate index because they have artifact/version identity but
    # no timestamp or speaker. Project eligible rows into the same ranking only
    # after applying the durable library boundary.
    note_stmt = (
        select(KnowledgeChunk, KnowledgeDocument)
        .join(
            KnowledgeDocument,
            KnowledgeDocument.id == KnowledgeChunk.document_id,
        )
        .outerjoin(PlaudFile, PlaudFile.id == KnowledgeDocument.file_id)
        .where(
            KnowledgeChunk.embedding.is_not(None),
            KnowledgeChunk.dim == dim,
            KnowledgeDocument.status == "completed",
        )
    )
    if file_id is not None:
        note_stmt = note_stmt.where(KnowledgeDocument.file_id == file_id)
    else:
        note_stmt = note_stmt.where(
            or_(
                KnowledgeDocument.file_id.is_(None),
                PlaudFile.is_trash.is_(False),
            )
        )
    if scope:
        # A library-level saved note has no recording metadata to prove that it
        # belongs inside a filtered boundary. It is eligible only when unscoped.
        note_stmt = note_stmt.where(KnowledgeDocument.file_id.is_not(None))
        if scope.get("speaker_name"):
            note_stmt = note_stmt.where(False)
        if scope.get("folder_id"):
            note_stmt = note_stmt.where(PlaudFile.folder_id == scope["folder_id"])
        if scope.get("tag_id"):
            note_stmt = note_stmt.where(PlaudFile.tags.any(Tag.id == scope["tag_id"]))
        if scope.get("origin") == "plaud":
            note_stmt = note_stmt.where(
                or_(PlaudFile.origin == "plaud", PlaudFile.origin.is_(None))
            )
        elif scope.get("origin") == "local":
            note_stmt = note_stmt.where(PlaudFile.origin == "local")
        if scope.get("date_from_ms") is not None:
            note_stmt = note_stmt.where(PlaudFile.start_time_ms >= scope["date_from_ms"])
        if scope.get("date_to_ms_exclusive") is not None:
            note_stmt = note_stmt.where(PlaudFile.start_time_ms < scope["date_to_ms_exclusive"])
        if scope.get("file_ids"):
            note_stmt = note_stmt.where(KnowledgeDocument.file_id.in_(scope["file_ids"]))

    from .knowledge_index import knowledge_document_is_current

    document_current: dict[int, bool] = {}
    for note_chunk, document in session.execute(note_stmt):
        if document.id not in document_current:
            document_current[document.id] = knowledge_document_is_current(session, document)
        if (
            document_current[document.id]
            and query_identity is not None
            and embedding_identity(document.profile_snapshot) == query_identity
        ):
            candidates.append({"kind": document.kind, "chunk": note_chunk, "document": document})
    if not candidates:
        return [], np.zeros((0, 0), dtype=np.float32)
    vecs = np.stack(
        [np.frombuffer(item["chunk"].embedding, dtype=np.float32) for item in candidates]
    )
    return candidates, vecs


def retrieve(
    query: str,
    top_k: int = 6,
    settings: Settings | None = None,
    file_id: str | None = None,
    retrieval_scope: dict | None = None,
    query_vector: np.ndarray | None = None,
    embedding_snapshot: dict | None = None,
    include_evidence_fingerprints: bool = False,
) -> list[dict]:
    """Return the top_k most relevant chunks as dicts with score + source.

    When ``file_id`` is provided, retrieval is scoped to that one recording.
    """
    settings = settings or get_settings()
    scope = normalize_library_scope(retrieval_scope)
    if embedding_snapshot is None:
        embedding_snapshot = _resolved_snapshot(file_id)
        selection = embedding_snapshot["stages"]["embed"]
        if selection.get("execution_target") == "remote_worker":
            query_vector = _remote_query_vector(query, embedding_snapshot, file_id)
        else:
            settings = _settings_for_stage(settings, embedding_snapshot, "embed")
    if query_vector is None:
        embedder = build_embedder(settings.embeddings)
        qv = np.asarray(embedder.embed([query])[0], dtype=np.float32)
    else:
        qv = np.asarray(query_vector, dtype=np.float32)
    if qv.ndim != 1 or not len(qv) or not np.isfinite(qv).all():
        raise ValueError("query embedding has an invalid vector shape")
    qn = qv / (np.linalg.norm(qv) + 1e-8)

    results: list[dict] = []
    with session_scope() as session:
        candidates, mat = _load_matrix(
            session,
            dim=len(qv),
            file_id=file_id,
            retrieval_scope=scope,
            embedding_snapshot=embedding_snapshot,
        )
        if not candidates:
            return []
        norms = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-8)
        scores = norms @ qn
        ranked = sorted(
            range(len(candidates)),
            key=lambda index: (
                -float(scores[index]),
                0 if candidates[index]["kind"] == "user_note" else 1,
            ),
        )
        top: list[int] = []
        document_counts: dict[int, int] = {}
        seen_note_text: dict[str, int] = {}
        for index in ranked:
            candidate = candidates[index]
            document = candidate["document"]
            if document is not None:
                count = document_counts.get(document.id, 0)
                if count >= 2:
                    continue
                artifact = (
                    session.get(UserNote, document.user_note_id)
                    if document.kind == "user_note"
                    else session.get(Summary, document.summary_id)
                )
                normalized = " ".join(
                    (artifact.content_md if artifact is not None else candidate["chunk"].text)
                    .casefold()
                    .split()
                )
                matching_document = seen_note_text.get(normalized)
                if matching_document is not None and matching_document != document.id:
                    continue
                seen_note_text[normalized] = document.id
                document_counts[document.id] = count + 1
            top.append(index)
            if len(top) >= top_k:
                break
        selected_file_ids = {
            (
                candidates[index]["chunk"].file_id
                if candidates[index]["kind"] == "transcript"
                else candidates[index]["document"].file_id
            )
            for index in top
        } - {None}
        files = {
            row.id: row
            for row in session.scalars(select(PlaudFile).where(PlaudFile.id.in_(selected_file_ids)))
        }
        selected_speakers = list(
            session.scalars(select(Speaker).where(Speaker.file_id.in_(selected_file_ids)))
        )
        stable_speaker_keys = {(speaker.file_id, speaker.key) for speaker in selected_speakers}
        speaker_names = {
            (speaker.file_id, speaker.key): speaker.display_name
            for speaker in selected_speakers
            if speaker.display_name
        }
        display_name_keys: dict[tuple[str, str], str | None] = {}
        for speaker in selected_speakers:
            if not speaker.display_name:
                continue
            key = (speaker.file_id, speaker.display_name)
            display_name_keys[key] = None if key in display_name_keys else speaker.key
        selected_documents = [
            candidates[index]["document"]
            for index in top
            if candidates[index]["document"] is not None
        ]
        summary_ids = sorted(
            {
                document.summary_id
                for document in selected_documents
                if document.kind == "generated_summary" and document.summary_id is not None
            }
        )
        user_note_ids = sorted(
            {
                document.user_note_id
                for document in selected_documents
                if document.kind == "user_note" and document.user_note_id is not None
            }
        )
        locked_summaries = {
            row.id: row
            for row in session.scalars(_ordered_artifact_lock_query(Summary, summary_ids))
        }
        locked_user_notes = {
            row.id: row
            for row in session.scalars(_ordered_artifact_lock_query(UserNote, user_note_ids))
        }
        from .knowledge_index import knowledge_document_is_current

        for i in top:
            candidate = candidates[int(i)]
            c = candidate["chunk"]
            document = candidate["document"]
            if document is not None:
                document = session.get(type(document), document.id, populate_existing=True)
                if document is None or not knowledge_document_is_current(session, document):
                    continue
                f = files.get(document.file_id)
                if document.kind == "generated_summary":
                    artifact = locked_summaries.get(document.summary_id)
                    if artifact is None:
                        continue
                    from ..note_history import latest_revision_number

                    title = artifact.title or artifact.template.replace("-", " ").title()
                    target = "generated_note"
                    artifact_version = artifact.restored_from_revision or (
                        latest_revision_number(session, artifact.file_id, artifact.template) + 1
                    )
                    url = (
                        f"/file/{document.file_id}/notes/generated/"
                        f"{quote(artifact.template, safe='')}/versions/{artifact_version}"
                    )
                    label = f"Generated note · {title}"
                    artifact_id = artifact.id
                else:
                    artifact = locked_user_notes.get(document.user_note_id)
                    if artifact is None:
                        continue
                    title = artifact.title
                    target = "saved_note"
                    url = f"/notes/{artifact.id}/versions/{artifact.version}"
                    label = f"Saved note · {title}"
                    artifact_id = artifact.id
                    artifact_version = artifact.version
                result = {
                    "target": target,
                    "score": float(scores[int(i)]),
                    "text": c.text,
                    "start": None,
                    "end": None,
                    "speaker": None,
                    "speaker_key": None,
                    "file_id": document.file_id,
                    "filename": f.display_title if f else title,
                    "artifact_id": artifact_id,
                    "artifact_title": title,
                    "artifact_version": artifact_version,
                    "url": url,
                    "label": label,
                }
                if include_evidence_fingerprints:
                    result["_evidence_fingerprint"] = _note_evidence_identity(
                        document,
                        artifact,
                        scope if file_id is None else None,
                    )
                results.append(result)
                continue
            f = files.get(c.file_id)
            stable_speaker = (
                c.speaker
                if c.speaker is not None and (c.file_id, c.speaker) in stable_speaker_keys
                else None
            )
            display_speaker = speaker_names.get((c.file_id, c.speaker))
            if display_speaker is None and c.speaker is not None:
                legacy_key = display_name_keys.get((c.file_id, c.speaker))
                if legacy_key is not None:
                    stable_speaker = legacy_key
                    display_speaker = c.speaker
            result = {
                "target": "transcript",
                "score": float(scores[int(i)]),
                "text": c.text,
                "start": c.start,
                "end": c.end,
                "speaker": display_speaker or c.speaker,
                "speaker_key": stable_speaker,
                "file_id": c.file_id,
                "filename": f.display_title if f else c.file_id,
                "artifact_id": None,
                "artifact_title": None,
                "artifact_version": None,
                "url": (
                    f"/file/{c.file_id}?t={c.start}"
                    if c.start is not None
                    else f"/file/{c.file_id}"
                ),
                "label": "Transcript",
            }
            if include_evidence_fingerprints:
                result["_evidence_fingerprint"] = _transcript_evidence_identity(
                    c,
                    scope if file_id is None else None,
                    [speaker for speaker in selected_speakers if speaker.file_id == c.file_id],
                    speaker_key=stable_speaker,
                    canonical_transcript_fingerprint=_canonical_transcript_fingerprint(
                        session, c
                    ),
                )
            results.append(result)
    return results


def _ordered_artifact_lock_query(model, artifact_ids: list[int]):
    return select(model).where(model.id.in_(artifact_ids)).order_by(model.id).with_for_update()


_QA_SYSTEM = (
    "You answer questions using only the provided transcript and note excerpts "
    "from the user's own library. Cite the recording or note titles you used. If the excerpts do "
    "not contain the answer, say so plainly."
)

_QA_SYSTEM_SINGLE = (
    "You answer questions about one of the user's own voice recordings, using "
    "only the provided transcript and note excerpts from it. Reference transcript "
    "moments by timestamp, but cite note evidence by its note title without inventing "
    "a timestamp. If the excerpts do not contain the "
    "answer, say so plainly rather than guessing."
)


def _format_context(hits: list[dict]) -> str:
    blocks = []
    for h in hits:
        stamp = f" @ {h['start']:.0f}s" if h.get("start") is not None else ""
        speaker = f" · {h['speaker']}" if h.get("speaker") else ""
        if h.get("target") in {"generated_note", "saved_note"}:
            evidence = h.get("label") or "Note"
            blocks.append(f"[{h['filename']} · {evidence}] {h['text']}")
        else:
            blocks.append(f"[{h['filename']}{stamp}{speaker}] {h['text']}")
    return "\n\n".join(blocks)


def _resolved_snapshot(file_id: str | None) -> dict:
    with session_scope() as session:
        if file_id is not None:
            return resolve_recording_profile(session, file_id).to_dict()
        return preview_resolution(session).to_dict()


def _remote_query_vector(query: str, snapshot: dict, file_id: str | None) -> np.ndarray:
    """Embed one Ask query through the selected remote-worker contract."""
    from .pipeline import _remote_json_input, _run_remote_stage

    transcript = {
        "segments": [
            {
                "text": query,
                "start": 0.0,
                "end": 1.0,
                "speaker": "QUERY",
                "words": [],
            }
        ],
        "language": None,
        "duration": 1.0,
        "provider": "localplaud-query",
        "model": None,
        "has_speakers": False,
    }
    payload = _run_remote_stage(
        f"ask-query-{file_id or 'library'}",
        snapshot,
        "embed",
        [_remote_json_input("transcript", transcript)],
    )
    chunks = payload.get("chunks") or []
    vectors = payload.get("vectors_base64") or []
    from .pipeline import _validate_remote_returned_model

    _validate_remote_returned_model(payload, snapshot, "embed")
    try:
        dim = int(payload.get("dim") or 0)
        blob = base64.b64decode(vectors[0], validate=True)
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("remote query embedding returned invalid vector metadata") from exc
    if (
        len(chunks) != 1
        or chunks[0].get("text") != query
        or dim <= 0
        or len(blob) != dim * np.dtype(np.float32).itemsize
    ):
        raise ValueError("remote query embedding returned an invalid vector shape")
    vector = np.frombuffer(blob, dtype=np.float32).copy()
    if not np.isfinite(vector).all():
        raise ValueError("remote query embedding returned a non-finite vector")
    return vector


def _candidate_cost(
    snapshot: dict,
    stage: str,
    usage: dict,
    spent_cost_usd: float,
    reservation_id: str | None = None,
    file_id: str | None = None,
) -> tuple[float, dict]:
    if reservation_id is not None:
        with session_scope() as session:
            lock_cost_budget(session, file_id)
            current = (
                resolve_recording_profile(session, file_id).to_dict()
                if file_id is not None
                else preview_resolution(session).to_dict()
            )
            authorized = {
                provider_dispatch_fingerprint(candidate, stage)
                for candidate in candidate_snapshots(current, stage)
            }
            if provider_dispatch_fingerprint(snapshot, stage) not in authorized:
                raise RuntimeError(
                    f"{stage} provider profile changed before dispatch; retry the request"
                )
            return reserve_provider_cost(
                session,
                reservation_id=reservation_id,
                file_id=file_id,
                operation=stage,
                snapshot=snapshot,
                usage=usage,
                additional_spent_usd=spent_cost_usd,
            )
    selection = snapshot["stages"][stage]
    with session_scope() as session:
        pricing = pricing_for_stage(session, snapshot, stage)
    ceiling = (snapshot.get("policy") or {}).get("cost_ceiling")
    external = selection.get("execution_target") in {"cloud", "remote_worker"}
    if ceiling is not None and external and not pricing:
        raise CostPolicyError(
            f"{stage} cost is unknown for {selection.get('connection')}:{selection.get('model')}"
        )
    projected = estimate_cost(usage, pricing)
    if ceiling is not None and spent_cost_usd + projected > float(ceiling) + 1e-12:
        raise CostPolicyError(
            f"{stage} would exceed the ${float(ceiling):.6g} Ask ceiling "
            f"(${spent_cost_usd:.6g} spent + ${projected:.6g} projected)"
        )
    return projected, pricing


def _retrieve_with_profile(
    query: str,
    top_k: int,
    settings: Settings,
    file_id: str | None,
    snapshot: dict,
    spent_cost_usd: float,
    retrieval_scope: dict | None,
    reservation_id: str | None = None,
) -> tuple[list[dict], dict, dict, float]:
    failures: list[dict] = []
    candidates = candidate_snapshots(snapshot, "embed")
    successful_queries = 0
    total_cost = 0.0
    combined_hits: list[dict] = []
    selected_with_hits: dict | None = None
    queried_profiles: list[dict] = []

    def finish(candidate: dict) -> tuple[list[dict], dict, dict, float]:
        deduplicated: dict[tuple, dict] = {}
        for hit in combined_hits:
            identity = (
                hit.get("target", "transcript"),
                hit.get("file_id"),
                hit.get("artifact_id"),
                hit.get("artifact_version"),
                hit.get("start"),
                hit.get("end"),
                hit.get("text"),
            )
            previous = deduplicated.get(identity)
            if previous is None:
                deduplicated[identity] = copy.deepcopy(hit)
            else:
                previous["score"] = float(previous.get("score") or 0) + float(hit.get("score") or 0)
                previous.setdefault("embedding_matches", []).extend(
                    copy.deepcopy(hit.get("embedding_matches") or [])
                )
        merged = sorted(
            deduplicated.values(),
            key=lambda item: float(item.get("score") or 0),
            reverse=True,
        )[:top_k]
        provenance = copy.deepcopy(selected_with_hits or candidate)
        provenance["fallback_failures"] = copy.deepcopy(failures)
        provenance["queried_profiles"] = copy.deepcopy(queried_profiles)
        aggregate_usage = normalize_usage(
            {
                "input_chars": len(query) * successful_queries,
                "requests": successful_queries,
            }
        )
        return merged, provenance, aggregate_usage, round(total_cost, 8)

    for index, candidate in enumerate(candidates):
        usage = normalize_usage({"input_chars": len(query), "requests": 1})
        try:
            candidate_settings = _settings_for_stage(settings, candidate, "embed")
            _projected, pricing = _candidate_cost(
                candidate,
                "embed",
                usage,
                spent_cost_usd + (0.0 if reservation_id else total_cost),
                reservation_id,
                file_id,
            )
            _before_provider_dispatch()
            selection = candidate["stages"]["embed"]
            query_vector = (
                _remote_query_vector(query, candidate, file_id)
                if selection.get("execution_target") == "remote_worker"
                else None
            )
            hits = retrieve(
                query,
                top_k=top_k,
                settings=candidate_settings,
                file_id=file_id,
                retrieval_scope=retrieval_scope,
                query_vector=query_vector,
                embedding_snapshot=candidate,
                include_evidence_fingerprints=True,
            )
            actual_cost = estimate_cost(usage, pricing)
            successful_queries += 1
            total_cost += actual_cost
            if hits and selected_with_hits is None:
                selected_with_hits = candidate
            selection = candidate["stages"]["embed"]
            embedding_identity = {
                key: selection.get(key)
                for key in ("connection", "model", "provider_type", "execution_target")
            }
            queried_profile = copy.deepcopy(candidate)
            queried_profile["hit_count"] = len(hits)
            queried_profiles.append(queried_profile)
            for rank, hit in enumerate(hits, start=1):
                fused = copy.deepcopy(hit)
                raw_score = float(hit.get("score") or 0)
                match = {
                    "identity": embedding_identity,
                    "rank": rank,
                    "score": raw_score,
                }
                fused["embedding_identity"] = embedding_identity
                fused["embedding_rank"] = rank
                fused["embedding_score"] = raw_score
                fused["embedding_matches"] = [match]
                fused["score"] = 1.0 / (60 + rank)
                combined_hits.append(fused)
            if index + 1 >= len(candidates):
                return finish(candidate)
        except Exception as exc:  # noqa: BLE001 - explicit retry classification
            retryable = is_retryable_fallback_error(exc)
            selection = candidate["stages"]["embed"]
            failures.append(
                {
                    "index": index,
                    "connection": selection["connection"],
                    "model": selection["model"],
                    "error": sanitize_error(exc, max_length=500),
                    "retryable": retryable,
                }
            )
            if combined_hits and (not retryable or index + 1 >= len(candidates)):
                return finish(candidate)
            if not retryable or index + 1 >= len(candidates):
                raise
    raise RuntimeError("no embedding candidate executed")


def _finalize_failed_reservations(reservation_ids: list[str]) -> None:
    """Close uncertain provider reservations without erasing possible spend."""
    with session_scope() as session:
        finalize_provider_cost_reservations(
            session,
            reservation_ids,
            status="failed",
        )


def answer(
    query: str,
    top_k: int = 6,
    settings: Settings | None = None,
    file_id: str | None = None,
    history: list[dict] | None = None,
    spent_cost_usd: float = 0.0,
    instruction: str | None = None,
    retrieval_scope: dict | None = None,
) -> dict:
    """Retrieve + answer. Returns {answer, sources}.

    When ``file_id`` is provided, both retrieval and the answer are scoped to a
    single recording, and the model is asked to reference moments by timestamp.
    """
    settings = settings or get_settings()
    scope = normalize_library_scope(retrieval_scope)
    if file_id is not None and scope:
        raise ValueError("single-recording Ask cannot use a library scope")
    snapshot = _resolved_snapshot(file_id)
    reservation_group = secrets.token_hex(24)
    reservation_ids = [
        f"{reservation_group}:embed",
        f"{reservation_group}:ask",
    ]
    try:
        hits, embed_snapshot, embed_usage, embed_cost = _retrieve_with_profile(
            query,
            top_k,
            settings,
            file_id,
            snapshot,
            spent_cost_usd,
            scope,
            reservation_ids[0],
        )
    except Exception:
        _finalize_failed_reservations(reservation_ids)
        raise
    if not hits:
        with session_scope() as session:
            reserved_cost = provider_cost_reservation_total(session, reservation_ids)
            finalize_provider_cost_reservations(session, reservation_ids, status="completed")
        durable_cost = max(embed_cost, reserved_cost)
        if file_id is not None:
            return {
                "answer": "This recording isn't indexed yet — process it first, then ask again.",
                "sources": [],
                "usage": {"embed": embed_usage},
                "estimated_cost_usd": durable_cost,
                "provenance": {"profile": embed_snapshot},
                "_cost_reservation_ids": reservation_ids,
            }
        return {
            "answer": "No indexed recordings yet — run the pipeline first.",
            "sources": [],
            "usage": {"embed": embed_usage},
            "estimated_cost_usd": durable_cost,
            "provenance": {"profile": embed_snapshot},
            "_cost_reservation_ids": reservation_ids,
        }
    system = _QA_SYSTEM_SINGLE if file_id is not None else _QA_SYSTEM
    prior = ""
    if history:
        bounded = history[-8:]
        turns = "\n".join(
            f"{item.get('role', 'user').title()}: {str(item.get('content', ''))[:2000]}"
            for item in bounded
        )
        prior = f"Conversation so far:\n---\n{turns}\n---\n\n"
    scope_note = f"Library retrieval scope: {scope}. Do not answer beyond it.\n" if scope else ""
    prompt = (
        f"{prior}Current question: {query}\n"
        f"{scope_note}"
        f"{f'Output instruction: {instruction}' if instruction else ''}\n\n"
        f"Excerpts:\n---\n{_format_context(hits)}\n---\n\n"
        "Answer the question grounded in the excerpts above."
    )
    failures: list[dict] = []
    for index, candidate in enumerate(candidate_snapshots(snapshot, "ask")):
        candidate_settings = _settings_for_stage(settings, candidate, "ask")
        projected_usage = normalize_usage(
            {"input_chars": len(prompt) + len(system), "output_tokens": 800, "requests": 1}
        )
        try:
            _projected, pricing = _candidate_cost(
                candidate,
                "ask",
                projected_usage,
                spent_cost_usd,
                reservation_ids[1],
                file_id,
            )
            def dispatch(candidate_settings=candidate_settings) -> str:
                llm = build_llm(candidate_settings.llm)
                return llm.complete(prompt, system=system, temperature=0.2, max_tokens=800)

            text = _dispatch_with_current_evidence(hits, dispatch)
            actual_usage = normalize_usage(
                {
                    "input_chars": len(prompt) + len(system),
                    "output_chars": len(text),
                    "requests": 1,
                }
            )
            llm_cost = estimate_cost(actual_usage, pricing)
            provenance = copy.deepcopy(candidate)
            provenance["fallback_failures"] = failures
            provenance["retrieval_profile"] = copy.deepcopy(embed_snapshot)
            embed_selection = (embed_snapshot.get("stages") or {}).get("embed")
            if embed_selection is not None:
                provenance.setdefault("stages", {})["embed"] = copy.deepcopy(embed_selection)
            selection = candidate["stages"]["ask"]
            with session_scope() as session:
                reserved_cost = provider_cost_reservation_total(session, reservation_ids)
                finalize_provider_cost_reservations(session, reservation_ids, status="completed")
            return {
                "answer": text,
                "sources": _public_hits(hits),
                "usage": {"embed": embed_usage, "ask": actual_usage},
                "estimated_cost_usd": max(round(embed_cost + llm_cost, 8), reserved_cost),
                "provenance": {
                    "provider": selection["connection"].split(":", 1)[-1],
                    "model": selection["model"],
                    "profile": provenance,
                },
                "_cost_reservation_ids": reservation_ids,
            }
        except Exception as exc:  # noqa: BLE001 - explicit retry classification
            retryable = is_retryable_fallback_error(exc)
            selection = candidate["stages"]["ask"]
            failures.append(
                {
                    "index": index,
                    "connection": selection["connection"],
                    "model": selection["model"],
                    "error": sanitize_error(exc, max_length=500),
                    "retryable": retryable,
                }
            )
            if not retryable or index + 1 >= len(candidate_snapshots(snapshot, "ask")):
                _finalize_failed_reservations(reservation_ids)
                raise
    _finalize_failed_reservations(reservation_ids)
    raise RuntimeError("no Ask candidate executed")
