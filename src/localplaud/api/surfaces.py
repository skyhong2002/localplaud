"""Library-level Web surfaces: whole-library Ask page, Search helpers, and the
shared Jinja helpers they use.

Routes here import the main application module lazily so this router can be
included from ``app.py`` without a circular import.
"""

from __future__ import annotations

import json
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from html import escape
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from markupsafe import Markup
from pydantic import BaseModel, Field
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


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


def library_search(
    q: str | None,
    folder: str | None = None,
    tag: str | None = None,
    origin: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    semantic: bool = True,
) -> dict:
    """Lexical + (optional) semantic search over titles, canonical transcripts,
    generated notes, and saved notes, grouped by recording. Shared by the
    /search page and the command palette's JSON endpoint."""
    from sqlalchemy import or_, select

    from ..date_filters import normalize_calendar_date, resolve_date_scope
    from ..db.models import PlaudFile, Tag
    from ..db.session import session_scope
    from .app import _workspace_timezone_name

    def optional_int(value: str | None) -> int | None:
        try:
            return int(value) if value else None
        except ValueError:
            return None

    def optional_date(value: str | None) -> str | None:
        if value in (None, ""):
            return None
        try:
            return normalize_calendar_date(value)
        except ValueError:
            return None

    normalized_from = optional_date(date_from)
    normalized_to = optional_date(date_to)
    invalid_date_filter = bool(
        (date_from not in (None, "") and normalized_from is None)
        or (date_to not in (None, "") and normalized_to is None)
    )
    invalid_date_range = bool(
        not invalid_date_filter
        and normalized_from
        and normalized_to
        and normalized_from > normalized_to
    )
    timezone_name = _workspace_timezone_name()
    date_scope = (
        {}
        if invalid_date_filter or invalid_date_range
        else resolve_date_scope(
            normalized_from,
            normalized_to,
            timezone_name,
        )
    )
    filters = {
        "folder_id": optional_int(folder),
        "tag_id": optional_int(tag),
        "origin": origin if origin in {"plaud", "local"} else None,
        "date_from_ms": date_scope.get("date_from_ms"),
        "date_to_ms": date_scope.get("date_to_ms_exclusive"),
    }
    groups: list[dict] = []
    if q and not invalid_date_filter and not invalid_date_range:
        from ..library_search import lexical_search
        from ..worker.qa import retrieve

        hits = lexical_search(q, **filters, limit=100)
        semantic_scope = {
            key: value
            for key, value in {
                "folder_id": filters["folder_id"],
                "tag_id": filters["tag_id"],
                "origin": filters["origin"],
            }.items()
            if value is not None
        } | date_scope
        try:
            if not semantic:
                raise LookupError("semantic search disabled for this request")
            semantic_hits = retrieve(
                q,
                top_k=30,
                retrieval_scope=semantic_scope or None,
            )
        except Exception:  # noqa: BLE001 - embeddings/provider may be unavailable
            semantic_hits = []
        with session_scope() as session:
            stmt = select(PlaudFile.id).where(PlaudFile.is_trash.is_(False))
            if filters["folder_id"] is not None:
                stmt = stmt.where(PlaudFile.folder_id == filters["folder_id"])
            if filters["tag_id"] is not None:
                stmt = stmt.where(PlaudFile.tags.any(Tag.id == filters["tag_id"]))
            if filters["origin"] == "plaud":
                stmt = stmt.where(or_(PlaudFile.origin == "plaud", PlaudFile.origin.is_(None)))
            elif filters["origin"] == "local":
                stmt = stmt.where(PlaudFile.origin == filters["origin"])
            if filters["date_from_ms"] is not None:
                stmt = stmt.where(PlaudFile.start_time_ms >= filters["date_from_ms"])
            if filters["date_to_ms"] is not None:
                stmt = stmt.where(PlaudFile.start_time_ms < filters["date_to_ms"])
            allowed_ids = set(session.scalars(stmt))
        seen = {
            (hit["file_id"], round(hit.get("start") or -1, 1), hit["text"][:80].casefold())
            for hit in hits
        }
        for hit in semantic_hits:
            if hit["file_id"] not in allowed_ids:
                continue
            hit = hit | {"kind": "semantic"}
            key = (
                hit["file_id"],
                round(hit.get("start") or -1, 1),
                hit["text"][:80].casefold(),
            )
            if key not in seen:
                hits.append(hit)
                seen.add(key)
        by_file: dict[str, dict] = {}
        for h in sorted(hits, key=lambda item: -item["score"]):
            g = by_file.setdefault(
                h["file_id"], {"file_id": h["file_id"], "filename": h["filename"], "hits": []}
            )
            g["hits"].append(h)
        groups = sorted(by_file.values(), key=lambda g: -max(x["score"] for x in g["hits"]))
        if groups:
            with session_scope() as session:
                meta_rows = session.scalars(
                    select(PlaudFile).where(PlaudFile.id.in_([g["file_id"] for g in groups]))
                )
                meta = {
                    row.id: {
                        "duration_ms": row.duration_ms,
                        "start_time_ms": row.start_time_ms,
                        "folder": row.folder.name if row.folder else None,
                    }
                    for row in meta_rows
                }
            for g in groups:
                g.update(meta.get(g["file_id"], {}))
    return {
        "groups": groups,
        "search_filters": {
            "folder": filters["folder_id"],
            "tag": filters["tag_id"],
            "origin": filters["origin"],
            "date_from": normalized_from or "",
            "date_to": normalized_to or "",
            "date_timezone": date_scope.get("date_timezone") or timezone_name,
            "invalid_date_filter": invalid_date_filter,
            "invalid_date_range": invalid_date_range,
        },
    }


def hit_href(file_id: str, hit: dict) -> str:
    target = hit.get("target")
    if hit.get("kind") == "title":
        return f"/file/{file_id}"
    if target == "generated_note":
        return f"/file/{file_id}?tab=notes&note=sum-{hit.get('artifact_id')}"
    if target == "saved_note":
        return f"/file/{file_id}?tab=notes&note_id={hit.get('artifact_id')}"
    if target == "mind_map":
        return f"/file/{file_id}?tab=mindmap"
    if hit.get("start") is not None:
        return f"/file/{file_id}?t={hit['start']}"
    return f"/file/{file_id}"


_HIT_KIND_LABELS = {
    "generated_note": "Note",
    "saved_note": "Saved note",
    "mind_map": "Mind map",
    "transcript": "Transcript",
}


@router.get("/api/search")
def search_api(
    q: str = "",
    folder: str | None = None,
    tag: str | None = None,
    origin: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
    semantic: bool = False,
) -> dict:
    """Flat "best matches" for the command palette: one row per recording with
    its strongest content hit, a plain-text snippet, and a deep link (timestamp,
    note, or mind map). Clients highlight terms themselves; nothing here is HTML."""
    from ..db.session import session_scope
    from ..i18n import translator
    from ..preferences import get_workspace_preferences

    limit = max(1, min(limit, 50))
    result = library_search(
        q[:200], folder, tag, origin, date_from, date_to, semantic=semantic
    )
    with session_scope() as session:
        translate = translator(get_workspace_preferences(session)["locale"])
    rows = []
    for group in result["groups"][:limit]:
        content = next((hit for hit in group["hits"] if hit.get("kind") != "title"), None)
        hit = content or group["hits"][0]
        if hit.get("kind") == "title":
            label = "Title"
        elif hit.get("kind") == "semantic" and hit.get("start") is None:
            label = "Related"
        else:
            label = _HIT_KIND_LABELS.get(hit.get("target"), "Note")
        rows.append(
            {
                "file_id": group["file_id"],
                "title": group["filename"],
                "href": hit_href(group["file_id"], hit),
                "kind": translate(label),
                "start": hit.get("start"),
                "speaker": hit.get("speaker"),
                "snippet": "" if hit.get("kind") == "title" else (hit.get("text") or "")[:240],
                "start_time_ms": group.get("start_time_ms"),
                "duration_ms": group.get("duration_ms"),
                "match_count": len(group["hits"]),
            }
        )
    return {"q": q, "results": rows, "filters": result["search_filters"]}



# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #

# Category -> (sprite icon, accent colour). Unknown categories use the default.
_TEMPLATE_GLYPHS = {
    "General": ("sparkles", "#8F53ED"),
    "Work": ("users", "#2F80ED"),
    "Meeting": ("users", "#2F80ED"),
    "Research": ("message", "#0E9F8E"),
    "Education": ("file-text", "#E07A1F"),
    "Functional": ("zap", "#C2410C"),
    "Transcription": ("waveform", "#5B6475"),
    "Custom": ("pencil", "#3D3D3D"),
}
_DEFAULT_GLYPH = ("template", "#5B6475")


def template_glyph(category: str | None) -> dict:
    icon_name, color = _TEMPLATE_GLYPHS.get(category or "", _DEFAULT_GLYPH)
    return {"icon": icon_name, "color": color}


def _provenance_label(item: dict) -> str:
    if item["is_builtin"]:
        return "Built-in"
    if item.get("provenance") == "personal-copy":
        return "Copied"
    if item.get("provenance") in {"imported", "plaud-import"}:
        return "Imported"
    if item.get("provenance") == "community":
        return "Community"
    return "Mine"


def template_library_context(tab: str, q: str, category: str | None) -> dict:
    """Data for the Templates page: My templates (recently used + personal) and
    Discover (bundled catalog by category), each with local usage counts."""
    from ..db.models import NoteTemplate, Summary
    from ..db.session import session_scope
    from .note_templates import _item

    tab = tab if tab in {"my", "explore"} else "my"
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(NoteTemplate)
                .where(NoteTemplate.is_active.is_(True))
                .order_by(NoteTemplate.is_builtin.desc(), NoteTemplate.name)
            )
        )
        usage_rows = session.execute(
            select(Summary.template, func.count(), func.max(Summary.created_at))
            .where(Summary.source == "local")
            .group_by(Summary.template)
        ).all()
    usage = {key: {"count": count, "last": last} for key, count, last in usage_rows}
    items = []
    for row in rows:
        item = _item(row)
        item["usage_count"] = usage.get(row.key, {}).get("count", 0)
        last_used = usage.get(row.key, {}).get("last")
        item["last_used"] = last_used.isoformat() if last_used is not None else None
        item["glyph"] = template_glyph(item["category"])
        item["provenance_label"] = _provenance_label(item)
        items.append(item)

    query = q.strip().casefold()

    def matches(item: dict) -> bool:
        if not query:
            return True
        haystack = " ".join(
            [item["name"], item["description"], item["category"], item["scenario"], item["author"]]
        ).casefold()
        return query in haystack

    personal = [item for item in items if not item["is_builtin"] and matches(item)]
    recent = sorted(
        (item for item in items if item["last_used"] is not None and matches(item)),
        key=lambda item: item["last_used"],
        reverse=True,
    )[:8]
    catalog = [item for item in items if item["is_builtin"] and matches(item)]
    categories = sorted({item["category"] for item in items if item["is_builtin"]})
    if category:
        catalog = [item for item in catalog if item["category"] == category]
    sections = []
    if not category and not query:
        popular = sorted(
            (item for item in catalog if item["usage_count"]),
            key=lambda item: -item["usage_count"],
        )[:8]
        if popular:
            sections.append({"title": "Most used in this workspace", "items": popular, "ranked": True})
        for name in categories:
            group = [item for item in catalog if item["category"] == name]
            if group:
                sections.append({"title": name, "items": group, "category": name})
    template_items = personal + [item for item in recent if item not in personal]
    template_items += [item for item in catalog if item not in template_items]
    return {
        "tab": tab,
        "q": q,
        "category": category,
        "categories": [
            {"name": name, "glyph": template_glyph(name)} for name in categories
        ],
        "personal_templates": personal,
        "recent_templates": recent,
        "catalog_templates": catalog,
        "catalog_sections": sections,
        # Everything rendered on the page, keyed for the detail/editor dialogs.
        "template_items": template_items,
    }


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def library_profile_stats(session) -> dict:
    """Profile header numbers (Plaud's Days / Recordings / Hours): days with at
    least one non-trash recording, counted in UTC calendar days."""
    from ..db.models import PlaudFile

    starts = session.scalars(
        select(PlaudFile.start_time_ms).where(
            PlaudFile.is_trash.is_(False), PlaudFile.start_time_ms.is_not(None)
        )
    )
    days = {int(ms // 86_400_000) for ms in starts}
    return {"days": len(days)}


def activity_heatmap(session, timezone_name: str, *, weeks: int = 40, today=None) -> dict:
    """Recordings per calendar day for the last ``weeks`` weeks (Plaud-style
    profile heat-map), as week columns of seven days (Monday first), each cell
    with a 0–4 intensity level. Days are computed in the workspace timezone."""
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from ..db.models import PlaudFile

    try:
        zone = ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001 - invalid zones fall back to UTC
        zone = UTC
    today = today or datetime.now(zone).date()
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=weeks - 1)
    start_ms = int(datetime.combine(start, datetime.min.time(), zone).timestamp() * 1000)
    counts: dict = {}
    for ms in session.scalars(
        select(PlaudFile.start_time_ms).where(
            PlaudFile.is_trash.is_(False), PlaudFile.start_time_ms >= start_ms
        )
    ):
        day = datetime.fromtimestamp(ms / 1000, zone).date()
        counts[day] = counts.get(day, 0) + 1
    peak = max(counts.values(), default=0)

    def level(count: int) -> int:
        if not count:
            return 0
        if peak <= 1:
            return 2
        return min(4, 1 + int(3 * (count - 1) / max(1, peak - 1) + 0.5))

    columns = []
    for week in range(weeks):
        column = []
        for weekday in range(7):
            day = start + timedelta(weeks=week, days=weekday)
            count = counts.get(day, 0) if day <= today else None
            column.append(
                {"date": day.isoformat(), "count": count, "level": level(count or 0)}
            )
        columns.append(column)
    return {
        "weeks": columns,
        "total": sum(counts.values()),
        "active_days": len(counts),
        "start": start.isoformat(),
        "end": today.isoformat(),
    }


def settings_phone_context(session, settings, timezone_name: str) -> dict:
    """Values for the phone Settings index and Preferences page."""
    from ..db.models import Notification, VocabularyTerm

    vocabulary_enabled = (
        session.scalar(
            select(func.count()).select_from(VocabularyTerm).where(VocabularyTerm.enabled.is_(True))
        )
        or 0
    )
    unread = (
        session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.read_at.is_(None), Notification.dismissed_at.is_(None))
        )
        or 0
    )
    return {
        "activity": activity_heatmap(session, timezone_name),
        "speech_prefs": {
            "language": settings.asr.language,
            "diarization": bool(settings.pipeline.diarize) and settings.diarize.provider != "none",
            "polish": bool(settings.pipeline.polish),
            "vocabulary_enabled": vocabulary_enabled,
            "unread_notifications": unread,
        },
    }



# --------------------------------------------------------------------------- #
# Streaming Ask (Server-Sent Events)
# --------------------------------------------------------------------------- #
#
# The non-streaming endpoints (POST /ask, /ask/skill, /file/{id}/ask, ...) stay
# unchanged. These variants run the same durable ``ask_in_thread`` call in a
# worker thread and forward progress as SSE events:
#
#   start {stream_id}      -> id usable with POST /api/ask/streams/{id}/cancel
#   sources {passages, recordings}
#   delta {text}           -> incremental answer text (whole answer at once for
#                             providers without streaming support)
#   reset {}               -> a fallback provider replaces a failed partial answer
#   done {html, thread_id, unavailable}
#   cancelled {} | error {status, message}
#
# Cancelling (explicitly or by disconnecting) stops generation before the
# answer is persisted, so a stopped question never creates a half message.

_ACTIVE_STREAMS: dict[str, object] = {}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _ask_event_stream(request: Request, run, render, unavailable):
    import queue
    import threading

    from ..worker import qa

    stream_id = secrets.token_urlsafe(12)
    cancel = threading.Event()
    _ACTIVE_STREAMS[stream_id] = cancel
    events: queue.Queue = queue.Queue()

    def on_sources(hits: list[dict]) -> None:
        events.put(("sources", source_summary(hits)))

    def worker() -> None:
        try:
            with qa.ask_stream_hooks(
                on_delta=lambda text: events.put(("delta", {"text": text})),
                on_sources=on_sources,
                on_reset=lambda: events.put(("reset", {})),
                cancel=cancel,
            ):
                thread = run()
            events.put(("thread", thread))
        except qa.AskCancelled:
            events.put(("cancelled", {}))
        except LookupError as exc:
            events.put(("error", {"status": 404, "message": str(exc)}))
        except ValueError as exc:
            events.put(("error", {"status": 409, "message": str(exc)}))
        except Exception:  # noqa: BLE001 - provider may be unavailable
            events.put(("unavailable", {}))
        finally:
            events.put(None)

    threading.Thread(target=worker, name=f"localplaud-ask-{stream_id}", daemon=True).start()

    def body():
        finished = False
        try:
            yield _sse("start", {"stream_id": stream_id})
            while True:
                try:
                    item = events.get(timeout=15)
                except queue.Empty:
                    yield ": keep-alive\n\n"
                    continue
                if item is None:
                    finished = True
                    break
                kind, payload = item
                if kind == "thread":
                    yield _sse(
                        "done",
                        {"thread_id": payload.get("thread_id"), "html": render(payload)},
                    )
                elif kind == "unavailable":
                    yield _sse(
                        "done", {"thread_id": None, "html": render(unavailable()), "unavailable": True}
                    )
                else:
                    yield _sse(kind, payload)
        finally:
            if not finished:
                cancel.set()
            _ACTIVE_STREAMS.pop(stream_id, None)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _render_fragment(request: Request, name: str, thread: dict, file_id: str | None, target: str):
    from . import app as main

    context = main._ask_fragment_context(request, thread, file_id, target)
    context["request"] = request
    return main.templates.get_template(name).render(context)


@router.post("/ask/stream")
def library_ask_stream(
    request: Request,
    q: str | None = Form(None),
    skill_key: str | None = Form(None),
    thread_id: str | None = Form(None),
    ask_folder_id: str | None = Form(None),
    ask_tag_id: str | None = Form(None),
    ask_origin: str | None = Form(None),
    ask_speaker_name: str | None = Form(None),
    ask_date_from: str | None = Form(None),
    ask_date_to: str | None = Form(None),
    ask_file_ids: Annotated[list[str] | None, Form()] = None,
    ui: str | None = Form(None),
):
    """Streaming variant of ``POST /ask`` / ``POST /ask/skill`` (library scope)."""
    from ..ask_skills import get_ask_skill
    from ..ask_threads import ask_in_thread
    from ..worker.qa import normalize_library_scope
    from . import app as main

    skill = None
    if skill_key:
        try:
            skill = get_ask_skill(skill_key, "library")
        except LookupError as exc:
            return HTMLResponse(str(exc), status_code=404)
    elif not (q or "").strip():
        return HTMLResponse("question must not be empty", status_code=422)
    try:
        scope = main._library_ask_scope(
            ask_folder_id,
            ask_tag_id,
            ask_origin,
            ask_speaker_name,
            ask_date_from,
            ask_date_to,
            main._workspace_timezone_name(),
            ask_file_ids,
        )
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=409)
    template, target = ("_ask_chat.html", "sf-thread") if ui == "chat" else ("_ask_thread.html", "answer")

    def run() -> dict:
        if skill is not None:
            return ask_in_thread(
                skill["retrieval_query"],
                display_query=skill["name"],
                instruction=skill["instruction"],
                skill_snapshot=skill,
                retrieval_scope=scope,
            )
        return ask_in_thread(q, thread_id=thread_id, retrieval_scope=scope)

    def unavailable() -> dict:
        try:
            fallback = normalize_library_scope(scope)
        except ValueError:
            fallback = {}
        return main._unavailable_ask_thread(
            skill["name"] if skill else q, None if skill else thread_id, retrieval_scope=fallback
        )

    return _ask_event_stream(
        request,
        run,
        lambda thread: _render_fragment(request, template, thread, None, target),
        unavailable,
    )


@router.post("/file/{file_id}/ask/stream")
def file_ask_stream(
    request: Request,
    file_id: str,
    q: str | None = Form(None),
    skill_key: str | None = Form(None),
    thread_id: str | None = Form(None),
):
    """Streaming variant of ``POST /file/{id}/ask`` and ``/file/{id}/ask/skill``.

    The final ``done`` event carries the same ``_ask_thread.html`` fragment the
    recording workspace already swaps into ``#file-answer``.
    """
    from ..ask_skills import get_ask_skill
    from ..ask_threads import ask_in_thread
    from ..db.models import PlaudFile
    from ..db.session import session_scope
    from . import app as main

    with session_scope() as session:
        if session.get(PlaudFile, file_id) is None:
            return HTMLResponse("Not found", status_code=404)
    skill = None
    if skill_key:
        try:
            skill = get_ask_skill(skill_key)
        except LookupError as exc:
            return HTMLResponse(str(exc), status_code=404)
    elif not (q or "").strip():
        return HTMLResponse("question must not be empty", status_code=422)

    def run() -> dict:
        if skill is not None:
            return ask_in_thread(
                skill["retrieval_query"],
                file_id=file_id,
                display_query=skill["name"],
                instruction=skill["instruction"],
                skill_snapshot=skill,
            )
        return ask_in_thread(q, file_id=file_id, thread_id=thread_id)

    def unavailable() -> dict:
        return main._unavailable_ask_thread(
            skill["name"] if skill else q, None if skill else thread_id, file_id=file_id
        )

    return _ask_event_stream(
        request,
        run,
        lambda thread: _render_fragment(request, "_ask_thread.html", thread, file_id, "file-answer"),
        unavailable,
    )


@router.post("/api/ask/streams/{stream_id}/cancel")
def cancel_ask_stream(stream_id: str) -> dict:
    """Stop an in-flight streaming Ask. Nothing from the stopped answer is saved."""
    cancel = _ACTIVE_STREAMS.get(stream_id)
    if cancel is None:
        return {"cancelled": False}
    cancel.set()
    return {"cancelled": True}



# --------------------------------------------------------------------------- #
# AutoFlow helpers: live sentence preview and drag-and-drop ordering
# --------------------------------------------------------------------------- #


class RuleOrderBody(BaseModel):
    rule_ids: list[int] = Field(min_length=1, max_length=500)


class SentencePreviewBody(BaseModel):
    trigger: dict = Field(default_factory=dict)
    actions: dict = Field(default_factory=dict)
    notify: bool = False


def _drop_empty(values: dict) -> dict:
    return {key: value for key, value in values.items() if value not in (None, "", [])}


@router.post("/api/automations/sentence-preview")
def automation_sentence_preview(body: SentencePreviewBody) -> dict:
    """Readable When/Then sentence for an unsaved rule (same wording as saved rules)."""
    from ..automations import rule_display_names, rule_sentence
    from ..db.session import session_scope
    from ..i18n import translator
    from ..preferences import get_workspace_preferences

    with session_scope() as session:
        translate = translator(get_workspace_preferences(session)["locale"])
        display_names = rule_display_names(session)
    rule = {
        "trigger": _drop_empty(body.trigger),
        "actions": _drop_empty(body.actions),
        "notify": body.notify,
    }
    try:
        sentence = rule_sentence(rule, translate=translate, names=display_names)
    except Exception:  # noqa: BLE001 - a half-filled form must never 500
        sentence = ""
    return {"sentence": sentence}


@router.post("/api/automations/rule-order")
def reorder_automation_rules(body: RuleOrderBody) -> dict:
    """Apply a new first-match order to locally owned rules.

    The rules keep the same set of priority numbers (so their position relative
    to externally owned rules is preserved); only rules whose priority changes
    get a new version. Externally owned rules cannot be reordered here.
    """
    from ..db.models import AutomationRule
    from ..db.session import session_scope

    if len(set(body.rule_ids)) != len(body.rule_ids):
        raise HTTPException(status_code=422, detail="rule ids must be unique")
    with session_scope() as session:
        rows = {
            row.id: row
            for row in session.scalars(
                select(AutomationRule).where(AutomationRule.id.in_(body.rule_ids))
            )
        }
        if set(rows) != set(body.rule_ids):
            raise HTTPException(status_code=404, detail="rule not found")
        if any((row.owner_type or "local") != "local" for row in rows.values()):
            raise HTTPException(
                status_code=409,
                detail="externally owned rules can only be reordered by their owner",
            )
        slots = sorted(row.priority for row in rows.values())
        assigned: list[int] = []
        for slot in slots:
            assigned.append(max(slot, assigned[-1] + 1) if assigned else slot)
        if assigned[-1] > 10_000:
            assigned = list(range(10, 10 * (len(assigned) + 1), 10))
        changed = []
        for rule_id, priority in zip(body.rule_ids, assigned, strict=True):
            row = rows[rule_id]
            if row.priority != priority:
                row.priority = priority
                row.version += 1
                changed.append(rule_id)
        session.flush()
        return {
            "rules": [
                {
                    "id": rule_id,
                    "priority": rows[rule_id].priority,
                    "version": rows[rule_id].version,
                }
                for rule_id in body.rule_ids
            ],
            "changed": changed,
        }
