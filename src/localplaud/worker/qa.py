"""Q&A and semantic search over the transcript knowledge base.

localplaud's answer to "Ask Plaud": embed the question, retrieve the closest
chunks by cosine similarity, and let the LLM answer grounded in them, citing
the source recordings.
"""

from __future__ import annotations

import copy
import logging
from datetime import UTC, date, datetime, timedelta

import numpy as np
from sqlalchemy import select

from ..config import Settings, get_settings
from ..db.models import Chunk, PlaudFile, Tag
from ..db.session import session_scope
from ..embeddings.base import build_embedder
from ..llm.base import build_llm
from ..providers.fallback import candidate_snapshots, is_retryable_fallback_error
from ..providers.service import preview_resolution, resolve_recording_profile
from ..providers.usage import CostPolicyError, estimate_cost, normalize_usage, pricing_for_stage
from .pipeline import _settings_for_stage

log = logging.getLogger(__name__)


def normalize_library_scope(value: dict | None) -> dict:
    """Validate and canonicalize a durable whole-library retrieval boundary."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("library Ask scope must be an object")
    unknown = sorted(
        set(value) - {"folder_id", "tag_id", "origin", "date_from", "date_to", "file_ids"}
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
    parsed_dates = {}
    for key in ("date_from", "date_to"):
        raw = value.get(key)
        if raw not in (None, ""):
            try:
                parsed_dates[key] = date.fromisoformat(str(raw))
            except ValueError as exc:
                raise ValueError(f"{key} must use YYYY-MM-DD") from exc
            scope[key] = parsed_dates[key].isoformat()
    if parsed_dates.get("date_from") and parsed_dates.get("date_to"):
        if parsed_dates["date_from"] > parsed_dates["date_to"]:
            raise ValueError("date_from must not follow date_to")
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


def _date_ms(value: str, *, exclusive_end: bool = False) -> int:
    parsed = datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)
    if exclusive_end:
        parsed += timedelta(days=1)
    return int(parsed.timestamp() * 1000)


def _load_matrix(
    session,
    dim: int,
    file_id: str | None = None,
    retrieval_scope: dict | None = None,
) -> tuple[list[Chunk], np.ndarray]:
    # Only chunks embedded at the query's dimension are comparable; mixing dims
    # (e.g. after switching embeddings.provider) would crash np.stack / the dot
    # product. Filter to the current embedder's dimension. When ``file_id`` is
    # set, scope retrieval to a single recording (single-file Ask).
    stmt = select(Chunk).where(Chunk.embedding.is_not(None), Chunk.dim == dim)
    if file_id is not None:
        stmt = stmt.where(Chunk.file_id == file_id)
    scope = normalize_library_scope(retrieval_scope)
    if file_id is not None and scope:
        raise ValueError("single-recording Ask cannot use a library scope")
    if scope:
        stmt = stmt.join(PlaudFile, PlaudFile.id == Chunk.file_id)
        if scope.get("folder_id"):
            stmt = stmt.where(PlaudFile.folder_id == scope["folder_id"])
        if scope.get("tag_id"):
            stmt = stmt.where(PlaudFile.tags.any(Tag.id == scope["tag_id"]))
        if scope.get("origin"):
            stmt = stmt.where(PlaudFile.origin == scope["origin"])
        if scope.get("date_from"):
            stmt = stmt.where(PlaudFile.start_time_ms >= _date_ms(scope["date_from"]))
        if scope.get("date_to"):
            stmt = stmt.where(
                PlaudFile.start_time_ms < _date_ms(scope["date_to"], exclusive_end=True)
            )
        if scope.get("file_ids"):
            stmt = stmt.where(PlaudFile.id.in_(scope["file_ids"]))
    chunks = list(session.scalars(stmt))
    if not chunks:
        return [], np.zeros((0, 0), dtype=np.float32)
    vecs = np.stack([np.frombuffer(c.embedding, dtype=np.float32) for c in chunks])
    return chunks, vecs


def retrieve(
    query: str,
    top_k: int = 6,
    settings: Settings | None = None,
    file_id: str | None = None,
    retrieval_scope: dict | None = None,
) -> list[dict]:
    """Return the top_k most relevant chunks as dicts with score + source.

    When ``file_id`` is provided, retrieval is scoped to that one recording.
    """
    settings = settings or get_settings()
    embedder = build_embedder(settings.embeddings)
    qv = np.asarray(embedder.embed([query])[0], dtype=np.float32)
    qn = qv / (np.linalg.norm(qv) + 1e-8)

    results: list[dict] = []
    with session_scope() as session:
        chunks, mat = _load_matrix(
            session, dim=len(qv), file_id=file_id, retrieval_scope=retrieval_scope
        )
        if not chunks:
            return []
        norms = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-8)
        scores = norms @ qn
        top = np.argsort(-scores)[:top_k]
        for i in top:
            c = chunks[int(i)]
            f = session.get(PlaudFile, c.file_id)
            results.append(
                {
                    "score": float(scores[int(i)]),
                    "text": c.text,
                    "start": c.start,
                    "end": c.end,
                    "speaker": c.speaker,
                    "file_id": c.file_id,
                    "filename": f.display_title if f else c.file_id,
                }
            )
    return results


_QA_SYSTEM = (
    "You answer questions using only the provided excerpts from the user's own "
    "voice recordings. Cite the recording titles you used. If the excerpts do "
    "not contain the answer, say so plainly."
)

_QA_SYSTEM_SINGLE = (
    "You answer questions about one of the user's own voice recordings, using "
    "only the provided excerpts from it. Reference the moments you rely on by "
    "their timestamp (e.g. \"around 2:30\"). If the excerpts do not contain the "
    "answer, say so plainly rather than guessing."
)


def _format_context(hits: list[dict]) -> str:
    blocks = []
    for h in hits:
        stamp = f" @ {h['start']:.0f}s" if h.get("start") is not None else ""
        blocks.append(f"[{h['filename']}{stamp}] {h['text']}")
    return "\n\n".join(blocks)


def _resolved_snapshot(file_id: str | None) -> dict:
    with session_scope() as session:
        if file_id is not None:
            return resolve_recording_profile(session, file_id).to_dict()
        return preview_resolution(session).to_dict()


def _candidate_cost(
    snapshot: dict,
    stage: str,
    usage: dict,
    spent_cost_usd: float,
) -> tuple[float, dict]:
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
) -> tuple[list[dict], dict, dict, float]:
    failures: list[dict] = []
    for index, candidate in enumerate(candidate_snapshots(snapshot, "embed")):
        candidate_settings = _settings_for_stage(settings, candidate, "embed")
        usage = normalize_usage({"input_chars": len(query), "requests": 1})
        try:
            _projected, pricing = _candidate_cost(
                candidate, "embed", usage, spent_cost_usd
            )
            hits = retrieve(
                query,
                top_k=top_k,
                settings=candidate_settings,
                file_id=file_id,
                retrieval_scope=retrieval_scope,
            )
            actual_cost = estimate_cost(usage, pricing)
            return hits, candidate, usage, actual_cost
        except Exception as exc:  # noqa: BLE001 - explicit retry classification
            retryable = is_retryable_fallback_error(exc)
            selection = candidate["stages"]["embed"]
            failures.append(
                {
                    "index": index,
                    "connection": selection["connection"],
                    "model": selection["model"],
                    "error": str(exc)[:500],
                    "retryable": retryable,
                }
            )
            if not retryable or index + 1 >= len(candidate_snapshots(snapshot, "embed")):
                raise
    raise RuntimeError("no embedding candidate executed")


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
    hits, embed_snapshot, embed_usage, embed_cost = _retrieve_with_profile(
        query, top_k, settings, file_id, snapshot, spent_cost_usd, scope
    )
    if not hits:
        if file_id is not None:
            return {
                "answer": "This recording isn't indexed yet — process it first, "
                "then ask again.",
                "sources": [],
                "usage": {"embed": embed_usage},
                "estimated_cost_usd": embed_cost,
                "provenance": {"profile": embed_snapshot},
            }
        return {
            "answer": "No indexed recordings yet — run the pipeline first.",
            "sources": [],
            "usage": {"embed": embed_usage},
            "estimated_cost_usd": embed_cost,
            "provenance": {"profile": embed_snapshot},
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
                spent_cost_usd + embed_cost,
            )
            llm = build_llm(candidate_settings.llm)
            text = llm.complete(prompt, system=system, temperature=0.2, max_tokens=800)
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
            selection = candidate["stages"]["ask"]
            return {
                "answer": text,
                "sources": hits,
                "usage": {"embed": embed_usage, "ask": actual_usage},
                "estimated_cost_usd": round(embed_cost + llm_cost, 8),
                "provenance": {
                    "provider": selection["connection"].split(":", 1)[-1],
                    "model": selection["model"],
                    "profile": provenance,
                },
            }
        except Exception as exc:  # noqa: BLE001 - explicit retry classification
            retryable = is_retryable_fallback_error(exc)
            selection = candidate["stages"]["ask"]
            failures.append(
                {
                    "index": index,
                    "connection": selection["connection"],
                    "model": selection["model"],
                    "error": str(exc)[:500],
                    "retryable": retryable,
                }
            )
            if not retryable or index + 1 >= len(candidate_snapshots(snapshot, "ask")):
                raise
    raise RuntimeError("no Ask candidate executed")
