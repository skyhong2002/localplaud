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


# Sprite icon name -> static SVG used when the shared sprite helper (``icon``,
# registered by the shell's ``ui.register``) is not installed. The shell's helper
# always wins: ``install`` only fills a missing global.
_FALLBACK_ICONS = {
    "search": "lucide/search", "x": "lucide/x", "plus": "lucide/plus",
    "pencil": "lucide/pencil", "trash": "lucide/trash-2", "more": "lucide/ellipsis",
    "history": "lucide/history", "filter": "lucide/list-filter", "sliders": "icons-sf/sliders",
    "chevron-down": "lucide/chevron-down", "chevron-left": "icons-sf/chevron-left",
    "chevron-right": "icons-sf/chevron-right", "arrow-up": "icons-sf/arrow-up",
    "sparkles": "lucide/sparkles", "check": "lucide/check", "copy": "lucide/copy",
    "file-text": "icons-sf/file-text", "play": "lucide/play", "note": "lucide/sticky-note",
    "alert-circle": "lucide/circle-alert", "alert": "lucide/triangle-alert",
    "waveform": "lucide/audio-lines", "mic": "icons-sf/mic", "user": "icons-sf/user",
    "template": "lucide/layout-template", "compass": "lucide/compass", "bell": "lucide/bell",
    "settings": "lucide/settings", "activity": "lucide/activity", "cloud": "lucide/cloud",
    "folder": "lucide/folder", "tag": "icons-sf/tag", "clock": "icons-sf/clock",
    "external-link": "icons-sf/external-link", "link": "lucide/link", "zap": "lucide/sparkles",
    "download": "lucide/download", "upload": "lucide/upload", "files": "lucide/files",
    "drive": "lucide/hard-drive", "refresh": "lucide/rotate-cw", "star": "lucide/sparkles",
    "info": "lucide/circle-alert", "message": "lucide/sticky-note", "users": "icons-sf/user", "eye": "icons-sf/eye-off",
    "monitor": "icons-sf/cpu", "keyboard": "icons-sf/sliders", "send": "icons-sf/arrow-up",
}


def fallback_icon(name: str, size: str | None = None, label: str | None = None, cls: str = "") -> Markup:
    path = _FALLBACK_ICONS.get(name, "lucide/sparkles")
    size_class = {"xs": "i-xs", "sm": "i-sm", "lg": "i-lg"}.get(size or "", "")
    a11y = f'role="img" aria-label="{escape(label)}"' if label else 'aria-hidden="true"'
    return Markup(
        f'<span class="nav-icon i {size_class} {escape(cls)}" {a11y} '
        f"style=\"--nav-icon:url('/static/{path}.svg')\"></span>"
    )


def sf_icon(name: str, size: str | None = None) -> Markup:
    """Icons not in the shared sprite (lock, shield, plug, ...), from static/icons-sf."""
    size_class = {"xs": "i-xs", "sm": "i-sm", "lg": "i-lg"}.get(size or "", "")
    safe = re.sub(r"[^a-z0-9-]", "", name)
    return Markup(
        f'<span class="nav-icon i {size_class}" aria-hidden="true" '
        f"style=\"--nav-icon:url('/static/icons-sf/{safe}.svg')\"></span>"
    )


def install(templates) -> None:
    templates.env.globals.setdefault("icon", fallback_icon)
    templates.env.globals["sf_icon"] = sf_icon
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
