"""Durable grounded Ask conversations and answer-to-note promotion."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from .config import Settings, get_settings
from .db.models import (
    AskMessage,
    AskThread,
    Folder,
    PlaudFile,
    Speaker,
    StageAttempt,
    Tag,
    UserNote,
)
from .db.session import session_scope
from .worker import qa


def _message(row: AskMessage) -> dict:
    profile = row.resolved_profile_snapshot or {}
    return {
        "id": row.id,
        "role": row.role,
        "content": row.content,
        "sources": row.sources or [],
        "provider": row.provider,
        "model": row.model,
        "usage": row.usage or {},
        "estimated_cost_usd": row.estimated_cost_usd or 0,
        "fallback": profile.get("fallback"),
        "skill_key": row.skill_key,
        "skill_snapshot": row.skill_snapshot,
    }


def thread_to_dict(row: AskThread) -> dict:
    return {
        "thread_id": row.id,
        "file_id": row.file_id,
        "title": row.title,
        "retrieval_scope": row.retrieval_scope or {},
        "messages": [_message(message) for message in row.messages],
    }


def ask_in_thread(
    query: str,
    *,
    file_id: str | None = None,
    thread_id: str | None = None,
    settings: Settings | None = None,
    display_query: str | None = None,
    instruction: str | None = None,
    skill_snapshot: dict | None = None,
    retrieval_scope: dict | None = None,
) -> dict:
    query = query.strip()
    if not query:
        raise ValueError("question must not be empty")
    settings = settings or get_settings()
    requested_scope = (
        qa.normalize_library_scope(retrieval_scope) if retrieval_scope is not None else None
    )
    if file_id is not None and requested_scope:
        raise ValueError("single-recording Ask cannot use a library scope")
    with session_scope() as session:
        if file_id is not None and session.get(PlaudFile, file_id) is None:
            raise LookupError("recording not found")
        thread = session.get(AskThread, thread_id) if thread_id else None
        if thread_id and thread is None:
            raise LookupError("thread not found")
        if thread is not None and thread.file_id != file_id:
            raise ValueError("thread scope does not match this Ask surface")
        stored_scope = (thread.retrieval_scope or {}) if thread is not None else {}
        if thread is not None and requested_scope is not None and requested_scope != stored_scope:
            raise ValueError("thread retrieval scope cannot change during follow-up")
        effective_scope = stored_scope if thread is not None else (requested_scope or {})
        if effective_scope.get("folder_id") and session.get(
            Folder, effective_scope["folder_id"]
        ) is None:
            raise ValueError("library Ask folder does not exist")
        if effective_scope.get("tag_id") and session.get(Tag, effective_scope["tag_id"]) is None:
            raise ValueError("library Ask tag does not exist")
        if effective_scope.get("speaker_name") and session.scalar(
            select(Speaker.id).where(
                Speaker.display_name.is_not(None),
                func.lower(Speaker.display_name)
                == effective_scope["speaker_name"].lower(),
            )
        ) is None:
            raise ValueError("library Ask named speaker does not exist")
        if effective_scope.get("file_ids"):
            known = set(
                session.scalars(
                    select(PlaudFile.id).where(PlaudFile.id.in_(effective_scope["file_ids"]))
                )
            )
            if known != set(effective_scope["file_ids"]):
                raise ValueError("library Ask contains an unknown recording")
        history = [_message(row) for row in thread.messages] if thread is not None else []
        ask_spent = sum(item.get("estimated_cost_usd", 0) for item in history)
        pipeline_spent = (
            float(
                session.scalar(
                    select(func.coalesce(func.sum(StageAttempt.estimated_cost_usd), 0)).where(
                        StageAttempt.file_id == file_id
                    )
                )
                or 0
            )
            if file_id is not None
            else 0.0
        )

    answer_kwargs = {
        "settings": settings,
        "file_id": file_id,
        "history": history,
        "spent_cost_usd": ask_spent + pipeline_spent,
        "instruction": instruction,
    }
    if effective_scope:
        answer_kwargs["retrieval_scope"] = effective_scope
    result = qa.answer(query, **answer_kwargs)
    with session_scope() as session:
        thread = session.get(AskThread, thread_id) if thread_id else None
        if thread_id and thread is None:
            raise LookupError("thread not found")
        if thread is None:
            thread = AskThread(
                id=str(uuid4()),
                file_id=file_id,
                title=(display_query or query)[:200],
                retrieval_scope=effective_scope,
            )
            session.add(thread)
            session.flush()
        thread.messages.extend(
            [
                AskMessage(
                    role="user",
                    content=display_query or query,
                    sources=[],
                    skill_key=(skill_snapshot or {}).get("key"),
                    skill_snapshot=skill_snapshot,
                ),
                AskMessage(
                    role="assistant",
                    content=result["answer"],
                    sources=result.get("sources", []),
                    provider=(result.get("provenance") or {}).get("provider"),
                    model=(result.get("provenance") or {}).get("model"),
                    resolved_profile_snapshot=(result.get("provenance") or {}).get(
                        "profile"
                    ),
                    usage=result.get("usage", {}),
                    estimated_cost_usd=result.get("estimated_cost_usd", 0),
                    skill_key=(skill_snapshot or {}).get("key"),
                    skill_snapshot=skill_snapshot,
                ),
            ]
        )
        thread.updated_at = datetime.now(UTC)
        session.flush()
        return thread_to_dict(thread)


def get_thread(thread_id: str, *, file_id: str | None = None) -> dict:
    with session_scope() as session:
        thread = session.get(AskThread, thread_id)
        if thread is None:
            raise LookupError("thread not found")
        if thread.file_id != file_id:
            raise ValueError("thread scope does not match this Ask surface")
        return thread_to_dict(thread)


def save_answer_as_note(message_id: int, title: str | None = None) -> dict:
    with session_scope() as session:
        message = session.get(AskMessage, message_id)
        if message is None or message.role != "assistant":
            raise LookupError("assistant answer not found")
        existing = session.scalar(
            select(UserNote).where(UserNote.ask_message_id == message_id)
        )
        if existing is not None:
            return note_to_dict(existing)
        thread = message.thread
        previous_question = next(
            (
                row.content
                for row in reversed(thread.messages)
                if row.id < message.id and row.role == "user"
            ),
            thread.title,
        )
        sources = message.sources or []
        source_file_ids = {item.get("file_id") for item in sources if item.get("file_id")}
        file_id = thread.file_id or (
            next(iter(source_file_ids)) if len(source_file_ids) == 1 else None
        )
        citations = []
        for item in sources:
            citations.append(
                {
                    key: item.get(key)
                    for key in ("file_id", "filename", "start", "end", "speaker", "text")
                }
            )
        note = UserNote(
            file_id=file_id,
            title=(title or previous_question or "Ask answer").strip()[:200],
            content_md=message.content,
            source_type="ask",
            ask_message_id=message.id,
            citations=citations,
        )
        session.add(note)
        session.flush()
        return note_to_dict(note)


def note_to_dict(row: UserNote) -> dict:
    return {
        "id": row.id,
        "file_id": row.file_id,
        "title": row.title,
        "content_md": row.content_md,
        "source_type": row.source_type,
        "ask_message_id": row.ask_message_id,
        "source_summary_id": row.source_summary_id,
        "citations": row.citations or [],
    }
