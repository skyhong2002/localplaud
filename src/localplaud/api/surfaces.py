"""Library-level Web surfaces: whole-library Ask page, Search helpers, and the
shared Jinja helpers they use.

Routes here import the main application module lazily so this router can be
included from ``app.py`` without a circular import.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup
from sqlalchemy import distinct, func, select

router = APIRouter(tags=["surfaces"])

_CITE_MARKER = re.compile(r"\[(\d{1,2})\](?![^<]*>)")
_READINESS_TIMEOUT_SECONDS = 4.0
_readiness_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="localplaud-ask-ready")


# --------------------------------------------------------------------------- #
# Jinja helpers
# --------------------------------------------------------------------------- #


def ask_cite(html: str, source_count: int) -> Markup:
    """Turn ``[n]`` markers in already-rendered answer HTML into citation pills.

    Only markers that refer to an existing source become buttons; anything else
    (``[99]``, markers inside tag attributes) is left untouched, so an answer can
    never point at a source that does not exist.
    """

    def replace(match: re.Match) -> str:
        number = int(match.group(1))
        if not 1 <= number <= source_count:
            return match.group(0)
        return (
            f'<button type="button" class="sf-cite" data-cite="{number}" '
            f'aria-label="Source {number}">{number}</button>'
        )

    return Markup(_CITE_MARKER.sub(replace, str(html)))


def highlight(text: str | None, query: str | None, limit: int | None = None) -> Markup:
    """Escape ``text`` and wrap case-insensitive occurrences of query terms in <mark>.

    When ``limit`` is set, the snippet is windowed around the first match so the
    highlighted term stays visible in long transcript chunks.
    """
    text = text or ""
    terms = [term for term in re.split(r"\s+", (query or "").strip()) if term][:8]
    if limit and len(text) > limit:
        first = None
        lowered = text.casefold()
        for term in terms:
            position = lowered.find(term.casefold())
            if position != -1 and (first is None or position < first):
                first = position
        start = 0 if first is None or first < limit // 3 else first - limit // 3
        end = start + limit
        text = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
    escaped = escape(text)
    if not terms:
        return Markup(escaped)
    pattern = re.compile(
        "|".join(re.escape(escape(term)) for term in sorted(terms, key=len, reverse=True)),
        re.IGNORECASE,
    )
    return Markup(pattern.sub(lambda match: f"<mark>{match.group(0)}</mark>", escaped))


def ask_followups(thread: dict | None, translate) -> list[str]:
    """Grounded "keep asking" prompts derived from the last answer's cited recordings."""
    if not thread:
        return []
    last = next(
        (message for message in reversed(thread.get("messages") or [])
         if message.get("role") == "assistant"),
        None,
    )
    if not last or last.get("unavailable") or not last.get("sources"):
        return []
    titles: list[str] = []
    for source in last["sources"]:
        title = (source.get("filename") or "").strip()
        if title and title not in titles:
            titles.append(title)
    if not titles:
        return []
    short = [title if len(title) <= 40 else f"{title[:39]}…" for title in titles]
    prompts = [
        translate("What else was discussed in “{title}”?").replace("{title}", short[0]),
        translate("List the action items from “{title}”").replace("{title}", short[0]),
    ]
    if len(short) > 1:
        prompts.append(
            translate("How do “{a}” and “{b}” differ?")
            .replace("{a}", short[0])
            .replace("{b}", short[1])
        )
    else:
        prompts.append(translate("Who said what about this, with timestamps?"))
    return prompts


def source_summary(sources: list[dict] | None) -> dict:
    sources = sources or []
    recordings = {source.get("file_id") for source in sources if source.get("file_id")}
    return {"passages": len(sources), "recordings": len(recordings)}


def install(templates) -> None:
    templates.env.filters["ask_cite"] = ask_cite
    templates.env.filters["highlight"] = highlight
    templates.env.filters["source_summary"] = source_summary
    templates.env.globals["ask_followups"] = ask_followups


# --------------------------------------------------------------------------- #
# Library Ask page
# --------------------------------------------------------------------------- #


def _index_stats(session) -> dict:
    from ..db.models import Chunk, PlaudFile

    indexed = (
        session.scalar(
            select(func.count(distinct(Chunk.file_id)))
            .select_from(Chunk)
            .join(PlaudFile, PlaudFile.id == Chunk.file_id)
            .where(PlaudFile.is_trash.is_(False))
        )
        or 0
    )
    total = (
        session.scalar(
            select(func.count()).select_from(PlaudFile).where(PlaudFile.is_trash.is_(False))
        )
        or 0
    )
    return {"indexed_recordings": indexed, "total_recordings": total}


@router.get("/ask", response_class=HTMLResponse)
def library_ask_page(
    request: Request,
    thread: str | None = None,
    ask_thread: str | None = None,
    q: str = "",
):
    from ..ask_skills import list_ask_skills
    from ..ask_threads import get_thread, list_threads
    from . import app as main

    thread_id = thread or ask_thread
    selected = None
    if thread_id:
        try:
            selected = get_thread(thread_id, file_id=None)
        except LookupError:
            selected = None
    with main.session_scope() as session:
        organization = main._organization_summary(session)
        named_speakers = main._named_speaker_summary(session)
        stats = _index_stats(session)
    history = list_threads(None, page_size=40)
    ctx = main._base_ctx(request, "ask") | {
        "organization": organization,
        "named_speakers": named_speakers,
        "ask_skills": list_ask_skills("library"),
        "thread": selected,
        "missing_thread": bool(thread_id and selected is None),
        "history": history,
        "prefill": q[:2000],
        "index_stats": stats,
        "file_id": None,
        "target": "sf-thread",
    }
    return main.templates.TemplateResponse(request=request, name="ask.html", context=ctx)


def _llm_health() -> dict:
    from ..config import get_settings
    from ..llm.base import build_llm

    settings = get_settings()
    provider = settings.llm.provider
    try:
        llm = build_llm(settings.llm)
        health = getattr(llm, "health", None)
        if callable(health):
            ok, detail = health()
        else:
            ok, detail = bool(llm.available()), ""
    except Exception as exc:  # noqa: BLE001 - surfaced as a degraded state
        ok, detail = False, str(exc)
    from ..error_redaction import sanitize_error

    return {"provider": provider, "ok": bool(ok), "detail": sanitize_error(str(detail or ""))[:160]}


@router.get("/api/ask/readiness")
def ask_readiness() -> dict:
    """Cheap, read-only readiness for the Ask surface: index coverage plus the
    default language model's health. Never sends recording data anywhere."""
    from ..db.session import session_scope

    with session_scope() as session:
        stats = _index_stats(session)
    future = _readiness_pool.submit(_llm_health)
    try:
        llm = future.result(timeout=_READINESS_TIMEOUT_SECONDS)
    except FutureTimeout:
        llm = {"provider": None, "ok": False, "detail": "health check timed out"}
    ready = bool(llm["ok"]) and stats["indexed_recordings"] > 0
    return stats | {"llm": llm, "ready": ready}
