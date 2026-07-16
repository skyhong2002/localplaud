"""Durable grounded Ask conversations and answer-to-note promotion."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import case, delete, exists, func, select, text, update
from sqlalchemy.orm import Session

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

_THREAD_PREVIEW_LENGTH = 180


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


def _exact_surface(file_id: str | None):
    """Match only the library or recording surface requested by the caller."""
    if file_id is None:
        return AskThread.file_id.is_(None)
    return AskThread.file_id == file_id


def _thread_for_surface(session: Session, thread_id: str, file_id: str | None) -> AskThread:
    thread = session.scalar(
        select(AskThread).where(AskThread.id == thread_id, _exact_surface(file_id))
    )
    if thread is None:
        raise LookupError("thread not found")
    return thread


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _serialize_saved_note_lifecycle(session: Session) -> None:
    # SQLite foreign keys are deliberately off in existing deployments. Take the
    # writer reservation before reading so answer promotion and thread deletion
    # cannot both commit around a stale ask_message_id.
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def list_threads(
    file_id: str | None,
    query: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """List Ask history for exactly one surface with aggregate thread metadata."""
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError("page must be a positive integer")
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or not 1 <= page_size <= 100
    ):
        raise ValueError("page_size must be an integer between 1 and 100")
    if not isinstance(query, str):
        raise ValueError("query must be a string")

    message_stats = (
        select(
            AskMessage.thread_id.label("thread_id"),
            func.count(AskMessage.id).label("message_count"),
            func.sum(case((AskMessage.role == "user", 1), else_=0)).label(
                "question_count"
            ),
        )
        .group_by(AskMessage.thread_id)
        .subquery()
    )
    note_stats = (
        select(
            AskMessage.thread_id.label("thread_id"),
            func.count(UserNote.id).label("saved_note_count"),
        )
        .select_from(AskMessage)
        .join(UserNote, UserNote.ask_message_id == AskMessage.id)
        .group_by(AskMessage.thread_id)
        .subquery()
    )
    last_message = (
        select(AskMessage.content)
        .where(AskMessage.thread_id == AskThread.id)
        .order_by(AskMessage.id.desc())
        .limit(1)
        .scalar_subquery()
    )

    filters = [_exact_surface(file_id)]
    normalized_query = query.strip()
    if normalized_query:
        pattern = f"%{_escape_like(normalized_query)}%"
        filters.append(
            AskThread.title.ilike(pattern, escape="\\")
            | exists().where(
                AskMessage.thread_id == AskThread.id,
                AskMessage.content.ilike(pattern, escape="\\"),
            )
        )

    with session_scope() as session:
        total = int(
            session.scalar(
                select(func.count(AskThread.id)).select_from(AskThread).where(*filters)
            )
            or 0
        )
        pages = max(1, (total + page_size - 1) // page_size)
        effective_page = min(page, pages)
        rows = session.execute(
            select(
                AskThread.id.label("thread_id"),
                AskThread.title,
                AskThread.file_id,
                AskThread.retrieval_scope,
                AskThread.created_at,
                AskThread.updated_at,
                func.coalesce(message_stats.c.message_count, 0).label("message_count"),
                func.coalesce(message_stats.c.question_count, 0).label("question_count"),
                func.substr(last_message, 1, _THREAD_PREVIEW_LENGTH).label(
                    "last_message_preview"
                ),
                func.coalesce(note_stats.c.saved_note_count, 0).label("saved_note_count"),
            )
            .outerjoin(message_stats, message_stats.c.thread_id == AskThread.id)
            .outerjoin(note_stats, note_stats.c.thread_id == AskThread.id)
            .where(*filters)
            .order_by(AskThread.updated_at.desc(), AskThread.id.desc())
            .offset((effective_page - 1) * page_size)
            .limit(page_size)
        ).mappings()
        threads = [
            {
                "thread_id": row["thread_id"],
                "title": row["title"],
                "file_id": row["file_id"],
                "retrieval_scope": row["retrieval_scope"] or {},
                "created_at": _utc_iso(row["created_at"]),
                "updated_at": _utc_iso(row["updated_at"]),
                "message_count": int(row["message_count"]),
                "question_count": int(row["question_count"]),
                "last_message_preview": row["last_message_preview"],
                "saved_note_count": int(row["saved_note_count"]),
            }
            for row in rows
        ]
    return {
        "threads": threads,
        "total": total,
        "page": effective_page,
        "page_size": page_size,
        "pages": pages,
    }


def rename_thread(thread_id: str, title: str, file_id: str | None) -> dict:
    if not isinstance(title, str):
        raise ValueError("title must be a string")
    normalized_title = title.strip()
    if not 1 <= len(normalized_title) <= 200:
        raise ValueError("title must be between 1 and 200 characters")
    with session_scope() as session:
        thread = _thread_for_surface(session, thread_id, file_id)
        thread.title = normalized_title
        thread.updated_at = datetime.now(UTC)
        session.flush()
        return {
            "thread_id": thread.id,
            "title": thread.title,
            "file_id": thread.file_id,
            "updated_at": _utc_iso(thread.updated_at),
        }


def delete_thread(thread_id: str, file_id: str | None) -> dict:
    with session_scope() as session:
        _serialize_saved_note_lifecycle(session)
        _thread_for_surface(session, thread_id, file_id)
        message_ids = select(AskMessage.id).where(AskMessage.thread_id == thread_id)
        deleted_message_count = int(
            session.scalar(
                select(func.count(AskMessage.id)).where(AskMessage.thread_id == thread_id)
            )
            or 0
        )
        detached_saved_note_count = int(
            session.scalar(
                select(func.count(UserNote.id)).where(UserNote.ask_message_id.in_(message_ids))
            )
            or 0
        )
        session.execute(
            update(UserNote)
            .where(UserNote.ask_message_id.in_(message_ids))
            .values(ask_message_id=None)
        )
        session.execute(delete(AskMessage).where(AskMessage.thread_id == thread_id))
        session.execute(
            delete(AskThread).where(AskThread.id == thread_id, _exact_surface(file_id))
        )
        return {
            "thread_id": thread_id,
            "deleted_message_count": deleted_message_count,
            "detached_saved_note_count": detached_saved_note_count,
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
        thread = _thread_for_surface(session, thread_id, file_id)
        return thread_to_dict(thread)


def save_answer_as_note(message_id: int, title: str | None = None) -> dict:
    with session_scope() as session:
        _serialize_saved_note_lifecycle(session)
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
        "source_summary_snapshot": row.source_summary_snapshot,
        "citations": row.citations or [],
    }
