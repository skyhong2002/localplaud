"""FastAPI app — browse recordings, read transcripts/summaries, search, ask."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy import text as sql_text
from sqlalchemy.orm import aliased
from starlette.background import BackgroundTask

from ..ask_skills import get_ask_skill, list_ask_skills
from ..ask_threads import thread_to_dict
from ..config import get_settings
from ..date_filters import calendar_date_ms, normalize_calendar_date, resolve_date_scope
from ..db.models import (
    AskThread,
    AutomationRule,
    AutomationRun,
    BrowserSession,
    Chunk,
    ExecutionProfile,
    FileStatus,
    Folder,
    ImportRun,
    NoteTemplate,
    Notification,
    PlaudFile,
    RecordingProfileOverride,
    Speaker,
    StageAttempt,
    StageName,
    StageRun,
    StageStatus,
    Summary,
    SummaryRevision,
    Tag,
    Transcript,
    TranscriptRevision,
    UserNote,
    VocabularyTerm,
    recording_tags,
)
from ..db.session import init_db, session_scope
from ..error_redaction import sanitize_error
from ..i18n import SUPPORTED_LOCALES, catalog, translator
from ..markdown import render_markdown as _render_markdown
from ..note_history import content_fingerprint, restore_summary_version
from ..preferences import (
    get_workspace_preferences,
    save_workspace_preferences,
    validate_timezone,
)
from ..remote.server import resume_pending_jobs
from ..remote.server import router as worker_router
from ..store.speakers import display_names, speaker_keys_from_segments
from .automations import router as automations_router
from .backups import router as backups_router
from .imports import router as imports_router
from .integrations import router as integrations_router
from .note_templates import _item as note_template_item
from .note_templates import router as note_templates_router
from .notes import router as notes_router
from .providers import router as providers_router
from .system import router as system_router
from .vocabulary import router as vocabulary_router

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
_NOTE_SOURCE_LABELS = {
    "manual": "Created by you",
    "ask": "Saved from Ask",
    "generated_summary": "Editable generated copy",
}


def _note_source_label(source_type: str) -> str:
    return _NOTE_SOURCE_LABELS.get(source_type, "Editable note")


def _escape_like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    from ..imports import recover_interrupted_imports

    recover_interrupted_imports()
    import threading

    threading.Thread(target=resume_pending_jobs, daemon=True).start()
    yield


app = FastAPI(title="localplaud", docs_url="/api/docs", lifespan=_lifespan)
app.include_router(providers_router)
app.include_router(vocabulary_router)
app.include_router(note_templates_router)
app.include_router(notes_router)
app.include_router(imports_router)
app.include_router(integrations_router)
app.include_router(automations_router)
app.include_router(backups_router)
app.include_router(worker_router)
app.include_router(system_router)

_static = _HERE / "static"
if _static.exists():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")

_SESSION_COOKIE = "localplaud_session"


def _session_hash(token: str, secret: str) -> str:
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _browser_session(token: str | None, secret: str | None) -> BrowserSession | None:
    if not token or not secret:
        return None
    now = datetime.now(UTC)
    with session_scope() as session:
        row = session.scalar(
            select(BrowserSession).where(
                BrowserSession.token_hash == _session_hash(token, secret),
                BrowserSession.expires_at > now,
            )
        )
        if row and now - _aware(row.last_seen_at) >= timedelta(minutes=5):
            row.last_seen_at = now
        if row:
            session.flush()
            session.expunge(row)
        return row


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _is_browser_navigation(request: Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


def _login_context(next_path: str, error: bool) -> dict:
    with session_scope() as session:
        preferences = get_workspace_preferences(session)
    return {
        "next": _safe_next(next_path),
        "error": error,
        "workspace_preferences": preferences,
        "t": translator(preferences["locale"]),
    }


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """Protect the Web App with a revocable opaque session and APIs with a token."""
    settings = get_settings().api
    token = settings.auth_token
    login_password = settings.login_password
    public_paths = {"/healthz", "/login"}
    if (
        (token or login_password)
        and request.url.path not in public_paths
        and not request.url.path.startswith("/static/")
        and not request.url.path.startswith("/api/worker/v1")
    ):
        supplied = request.headers.get("x-auth-token") or request.query_params.get("token")
        bearer = request.headers.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            supplied = bearer[7:]
        token_ok = bool(token and supplied and hmac.compare_digest(supplied, token))
        browser_session = (
            _browser_session(request.cookies.get(_SESSION_COOKIE), settings.session_secret)
            if login_password
            else None
        )
        session_ok = bool(browser_session)
        request.state.browser_session_id = browser_session.id if browser_session else None
        if not token_ok and not session_ok:
            if login_password:
                next_path = request.url.path
                if request.url.query:
                    next_path += f"?{request.url.query}"
                login_url = f"/login?next={quote(next_path, safe='')}"
                if request.headers.get("hx-request", "").lower() == "true":
                    return Response(status_code=401, headers={"HX-Redirect": login_url})
                if _is_browser_navigation(request):
                    return RedirectResponse(url=login_url, status_code=303)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", error: str | None = None):
    settings = get_settings().api
    if not settings.login_password:
        return RedirectResponse(url="/", status_code=303)
    if _browser_session(request.cookies.get(_SESSION_COOKIE), settings.session_secret):
        return RedirectResponse(url=_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=_login_context(next, bool(error)),
    )


@app.post("/login")
def login_submit(request: Request, password: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
    settings = get_settings().api
    if not settings.login_password or not settings.session_secret:
        raise HTTPException(status_code=503, detail="Web login is not configured")
    if not hmac.compare_digest(password, settings.login_password):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=_login_context(next, True),
            status_code=401,
        )
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    with session_scope() as session:
        session.execute(delete(BrowserSession).where(BrowserSession.expires_at <= now))
        session.add(
            BrowserSession(
                token_hash=_session_hash(token, settings.session_secret),
                user_agent=request.headers.get("user-agent", "")[:256],
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(seconds=settings.session_max_age_seconds),
            )
        )
    response = RedirectResponse(url=_safe_next(next), status_code=303)
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    settings = get_settings().api
    token = request.cookies.get(_SESSION_COOKIE)
    if token and settings.session_secret:
        with session_scope() as session:
            session.execute(
                delete(BrowserSession).where(
                    BrowserSession.token_hash == _session_hash(token, settings.session_secret)
                )
            )
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(
        _SESSION_COOKIE,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


@app.post("/api/sessions/{session_id}/revoke")
def revoke_browser_session(session_id: int, request: Request) -> dict:
    with session_scope() as session:
        row = session.get(BrowserSession, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Session not found")
        session.delete(row)
    return {"ok": True, "current": getattr(request.state, "browser_session_id", None) == session_id}


# --------------------------------------------------------------------------- #
# template helpers
# --------------------------------------------------------------------------- #


@pass_context
def _fmt_dt(context, ms: int | None) -> str:
    if not ms:
        return ""
    from datetime import datetime

    preferences = context.get("workspace_preferences") or {}
    timezone = ZoneInfo(preferences.get("timezone", "UTC"))
    hour_cycle = preferences.get("hour_cycle", "24")
    locale = preferences.get("locale", "en")
    if locale == "zh-Hant-TW":
        pattern = "%Y年%m月%d日 · %I:%M %p" if hour_cycle == "12" else "%Y年%m月%d日 · %H:%M"
    else:
        pattern = "%b %d, %Y · %I:%M %p" if hour_cycle == "12" else "%b %d, %Y · %H:%M"
    return datetime.fromtimestamp(ms / 1000, tz=UTC).astimezone(timezone).strftime(pattern)


def _fmt_dur(ms: int | None) -> str:
    if not ms:
        return "—"
    s = int(ms // 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _mmss(seconds) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["dur"] = _fmt_dur
templates.env.filters["mmss"] = _mmss
templates.env.filters["markdown"] = _render_markdown


def _file_summary(r: PlaudFile) -> dict:
    settings = get_settings()
    independent = settings.pipeline.artifact_mode == "independent"
    transcript = r.local_transcript if independent else r.transcript
    return {
        "id": r.id,
        "filename": r.display_title,
        "cloud_filename": r.filename,
        "local_title": r.local_title,
        "status": r.status.value,
        "duration_ms": r.duration_ms,
        "start_time_ms": r.start_time_ms,
        "scene": r.scene,
        "scene_label": _scene_label(r.scene),
        "is_trash": r.is_trash,
        "needs_attention": r.status.value in _ATTENTION_STATES,
        "retry": {
            "count": r.pipeline_retry_count or 0,
            "maximum": settings.pipeline.retry_max_attempts,
            "next_at": (r.pipeline_next_retry_at.isoformat() if r.pipeline_next_retry_at else None),
            "exhausted": (
                r.status.value in _ATTENTION_STATES
                and (r.pipeline_retry_count or 0) >= settings.pipeline.retry_max_attempts
            ),
        },
        "has_transcript": transcript is not None,
        "has_imported_transcript": r.plaud_transcript is not None,
        "has_summary": any(s.source == "local" for s in r.summaries),
        "has_imported_summary": any(s.source in {"cloud", "plaud"} for s in r.summaries),
        "has_audio": bool(r.audio_path),
        "origin": r.origin or "plaud",
        "speakers": transcript.has_speakers if transcript else False,
        "folder": (
            {"id": r.folder.id, "name": r.folder.name, "color": r.folder.color}
            if r.folder is not None
            else None
        ),
        "tags": [
            {"id": tag.id, "name": tag.name, "color": tag.color}
            for tag in sorted(r.tags, key=lambda tag: (tag.name.casefold(), tag.id))
        ],
    }


def _file_summaries(session, rows: list[PlaudFile]) -> list[dict]:
    """Build list rows in bounded queries without loading transcript/summary payloads."""
    if not rows:
        return []
    ids = [row.id for row in rows]
    transcript_flags: dict[str, dict[str, bool]] = {file_id: {} for file_id in ids}
    for file_id, source, has_speakers in session.execute(
        select(Transcript.file_id, Transcript.source, Transcript.has_speakers)
        .where(Transcript.file_id.in_(ids))
        .order_by(Transcript.id)
    ):
        transcript_flags[file_id][source] = bool(has_speakers)

    summary_sources: dict[str, set[str]] = {file_id: set() for file_id in ids}
    for file_id, source in session.execute(
        select(Summary.file_id, Summary.source).where(Summary.file_id.in_(ids))
    ):
        summary_sources[file_id].add(source)

    tag_map: dict[str, list[dict]] = {file_id: [] for file_id in ids}
    for file_id, tag_id, name, color in session.execute(
        select(recording_tags.c.file_id, Tag.id, Tag.name, Tag.color)
        .join(Tag, Tag.id == recording_tags.c.tag_id)
        .where(recording_tags.c.file_id.in_(ids))
        .order_by(func.lower(Tag.name), Tag.id)
    ):
        tag_map[file_id].append({"id": tag_id, "name": name, "color": color})

    folder_ids = {row.folder_id for row in rows if row.folder_id is not None}
    folder_map = {
        folder.id: {"id": folder.id, "name": folder.name, "color": folder.color}
        for folder in session.scalars(select(Folder).where(Folder.id.in_(folder_ids)))
    }
    settings = get_settings()
    independent = settings.pipeline.artifact_mode == "independent"
    result = []
    for row in rows:
        sources = transcript_flags[row.id]
        local_speakers = sources.get("local")
        imported_speakers = next(
            (sources[source] for source in ("plaud", "cloud") if source in sources), None
        )
        canonical_speakers = local_speakers if local_speakers is not None else imported_speakers
        local_summary = "local" in summary_sources[row.id]
        result.append(
            {
                "id": row.id,
                "filename": row.display_title,
                "cloud_filename": row.filename,
                "local_title": row.local_title,
                "status": row.status.value,
                "duration_ms": row.duration_ms,
                "start_time_ms": row.start_time_ms,
                "scene": row.scene,
                "scene_label": _scene_label(row.scene),
                "is_trash": row.is_trash,
                "needs_attention": row.status.value in _ATTENTION_STATES,
                "retry": {
                    "count": row.pipeline_retry_count or 0,
                    "maximum": settings.pipeline.retry_max_attempts,
                    "next_at": (
                        row.pipeline_next_retry_at.isoformat()
                        if row.pipeline_next_retry_at
                        else None
                    ),
                    "exhausted": (
                        row.status.value in _ATTENTION_STATES
                        and (row.pipeline_retry_count or 0)
                        >= settings.pipeline.retry_max_attempts
                    ),
                },
                "has_transcript": local_speakers is not None
                if independent
                else canonical_speakers is not None,
                "has_imported_transcript": imported_speakers is not None,
                "has_summary": local_summary,
                "has_imported_summary": bool(summary_sources[row.id] & {"cloud", "plaud"}),
                "has_audio": bool(row.audio_path),
                "origin": row.origin or "plaud",
                "speakers": bool(canonical_speakers),
                "folder": folder_map.get(row.folder_id),
                "tags": tag_map[row.id],
            }
        )
    return result


def _base_ctx(request: Request, active: str) -> dict:
    partial_response = request.headers.get("hx-request", "").lower() == "true" and (
        request.headers.get("hx-target") == "app-view"
        or request.headers.get("hx-history-restore-request", "").lower() == "true"
    )
    with session_scope() as session:
        unread_notifications = session.scalar(
            select(func.count()).select_from(Notification).where(
                Notification.read_at.is_(None), Notification.dismissed_at.is_(None)
            )
        ) or 0
        workspace_preferences = get_workspace_preferences(session)
        organization = _organization_summary(session)
        visible_filter = PlaudFile.is_trash.is_(False)
        status_counts = dict(
            session.execute(
                select(PlaudFile.status, func.count())
                .where(visible_filter)
                .group_by(PlaudFile.status)
            ).all()
        )
        sidebar_ops = {
            "ready": status_counts.get(FileStatus.done, 0),
            "attention": status_counts.get(FileStatus.error, 0)
            + status_counts.get(FileStatus.partial, 0),
            # Matches the workspace's pending vocabulary: actively working plus
            # everything queued for it — downloaded/downloading audio and
            # discovered rows the poller downloads automatically.
            "processing": status_counts.get(FileStatus.processing, 0)
            + status_counts.get(FileStatus.downloading, 0)
            + status_counts.get(FileStatus.downloaded, 0)
            + status_counts.get(FileStatus.discovered, 0),
            # Cloud-only rows are not "caught up" — they stay in Plaud until
            # the user imports audio; only metadata_only truly awaits a manual
            # import.
            "cloud": status_counts.get(FileStatus.metadata_only, 0),
        }
        sidebar_counts = {
            "all": session.scalar(
                select(func.count()).select_from(PlaudFile).where(visible_filter)
            )
            or 0,
            "uncategorized": session.scalar(
                select(func.count())
                .select_from(PlaudFile)
                .where(
                    visible_filter,
                    PlaudFile.folder_id.is_(None),
                    ~PlaudFile.tags.any(),
                )
            )
            or 0,
            "trash": session.scalar(
                select(func.count())
                .select_from(PlaudFile)
                .where(PlaudFile.is_trash.is_(True))
            )
            or 0,
            "plaud": session.scalar(
                select(func.count())
                .select_from(PlaudFile)
                .where(
                    visible_filter,
                    or_(PlaudFile.origin == "plaud", PlaudFile.origin.is_(None)),
                )
            )
            or 0,
            "local": session.scalar(
                select(func.count())
                .select_from(PlaudFile)
                .where(visible_filter, PlaudFile.origin == "local")
            )
            or 0,
        }
        sidebar_scenes = [
            {
                "value": scene if scene is not None else "unknown",
                "label": _scene_label(scene),
                "label_short": _scene_label_short(scene),
                "count": count,
            }
            for scene, count in session.execute(
                select(PlaudFile.scene, func.count())
                .where(visible_filter)
                .group_by(PlaudFile.scene)
                .order_by(PlaudFile.scene)
            )
        ]
    return {
        "request": request,
        "active": active,
        "public_url": get_settings().api.public_url,
        "web_login_configured": bool(get_settings().api.login_password),
        "unread_notifications": unread_notifications,
        "sidebar": {
            "folders": organization["folders"],
            "counts": sidebar_counts,
            "scenes": sidebar_scenes,
            "ops": sidebar_ops,
        },
        "workspace_preferences": workspace_preferences,
        "supported_locales": SUPPORTED_LOCALES,
        "t": translator(workspace_preferences["locale"]),
        "translations": catalog(workspace_preferences["locale"]),
        "partial_response": partial_response,
    }


def _workspace_timezone_name() -> str:
    with session_scope() as session:
        return str(get_workspace_preferences(session)["timezone"])


# --------------------------------------------------------------------------- #
# library sorting / filtering
# --------------------------------------------------------------------------- #

_SORT_COLUMNS = {
    "recorded": PlaudFile.start_time_ms,
    "name": func.coalesce(PlaudFile.local_title, PlaudFile.filename),
    "duration": PlaudFile.duration_ms,
}
_STATE_VALUES = {s.value for s in FileStatus}
_ATTENTION_STATES = {FileStatus.error.value, FileStatus.partial.value}
# Aggregate filters matching the Workspace-status vocabulary: one URL per
# ops-card bucket, resolved onto the same status filtering as single values.
_STATE_ALIASES = {
    "attention": [FileStatus.error, FileStatus.partial],
    # discovered is queued for automatic download (download_pending consumes
    # it), so it belongs to the pending pipeline, not the manual-import bucket.
    "generating": [
        FileStatus.processing,
        FileStatus.downloading,
        FileStatus.downloaded,
        FileStatus.discovered,
    ],
    "cloud": [FileStatus.metadata_only],
}


def _scene_label(scene: int | None) -> str:
    if scene is None:
        return "Unknown capture source"
    return f"Capture source {scene}"


def _scene_label_short(scene: int | None) -> str:
    # Sidebar item text under the "Sources" group header: the long form
    # ellipsizes into identical, indistinguishable entries at rail width.
    if scene is None:
        return "Unknown source"
    return f"Source {scene}"


def _parse_library_params(
    q: str | None,
    sort: str | None,
    dir: str | None,
    state: str | None,
    scene: str | None,
    view: str | None,
    folder: str | None = None,
    tag: str | None = None,
    origin: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_duration_minutes: str | None = None,
    max_duration_minutes: str | None = None,
) -> dict:
    """Normalize library query params, falling back to defaults on bad input."""
    sort_key = sort if sort in _SORT_COLUMNS else "recorded"
    direction = dir if dir in {"asc", "desc"} else "desc"
    state_val = state if state in _STATE_VALUES or state in _STATE_ALIASES else None
    scene_val: int | Literal["unknown"] | None = None
    if scene not in (None, ""):
        if scene == "unknown":
            scene_val = "unknown"
        else:
            try:
                scene_val = int(scene)
            except (TypeError, ValueError):
                scene_val = None
    view_val = view if view in {"all", "trash", "uncategorized"} else "all"

    def optional_int(value: str | None) -> int | None:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def optional_date(value: str | None) -> str | None:
        if value in (None, ""):
            return None
        try:
            return normalize_calendar_date(value)
        except ValueError:
            return None

    def optional_minutes(value: str | None) -> float | None:
        if value in (None, ""):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed) or parsed < 0 or parsed > 1_000_000:
            return None
        return parsed

    date_from_val = optional_date(date_from)
    date_to_val = optional_date(date_to)
    min_duration_val = optional_minutes(min_duration_minutes)
    max_duration_val = optional_minutes(max_duration_minutes)

    return {
        "q": q or "",
        "sort": sort_key,
        "dir": direction,
        "state": state_val,
        "scene": scene_val,
        "view": view_val,
        "folder": optional_int(folder),
        "tag": optional_int(tag),
        "origin": origin if origin in {"plaud", "local"} else None,
        "date_from": date_from_val,
        "date_to": date_to_val,
        "min_duration_minutes": min_duration_val,
        "max_duration_minutes": max_duration_val,
        "invalid_date_range": bool(date_from_val and date_to_val and date_from_val > date_to_val),
        "invalid_duration_range": bool(
            min_duration_val is not None
            and max_duration_val is not None
            and min_duration_val > max_duration_val
        ),
    }


def _library_return_url(request: Request) -> str:
    query = urlencode(
        [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key not in {"workspace", "preserve_filelist"}
        ]
    )
    return f"/{'?' + query if query else ''}"


def _validated_library_return_url(value: str | None) -> str:
    if not value:
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.path != "/" or parsed.fragment:
        return "/"
    return f"/{'?' + parsed.query if parsed.query else ''}"


def _library_state_from_return_url(value: str) -> tuple[dict, int]:
    """Recover the validated library filters/page used to open a recording."""
    query = parse_qs(urlsplit(value).query, keep_blank_values=True)

    def first(key: str) -> str | None:
        values = query.get(key)
        return values[0] if values else None

    params = _parse_library_params(
        first("q"),
        first("sort"),
        first("dir"),
        first("state"),
        first("scene"),
        first("view"),
        first("folder"),
        first("tag"),
        first("origin"),
        first("date_from"),
        first("date_to"),
        first("min_duration_minutes"),
        first("max_duration_minutes"),
    )
    try:
        page = max(1, int(first("page") or 1))
    except ValueError:
        page = 1
    return params, page


def _library_return_url_for_page(value: str, page: int) -> str:
    """Change only the page in a previously validated library return URL."""
    pairs = [(key, item) for key, item in parse_qsl(urlsplit(value).query) if key != "page"]
    if page > 1:
        pairs.append(("page", str(page)))
    query = urlencode(pairs)
    return f"/{'?' + query if query else ''}"


def _file_workspace_url(
    file_id: str,
    return_to: str,
    *,
    tab: str,
    view: str | None = None,
    ask_thread: str | None = None,
    revision: int | None = None,
    note_id: int | None = None,
    t: int | None = None,
    segment_error: str | None = None,
) -> str:
    """Build a side-list link without dropping the open workspace context."""
    pairs: list[tuple[str, str | int]] = [("return_to", return_to), ("tab", tab)]
    for key, value in (
        ("view", view),
        ("ask_thread", ask_thread),
        ("revision", revision),
        ("note_id", note_id),
        ("t", t),
        ("segment_error", segment_error),
    ):
        if value is not None:
            pairs.append((key, value))
    return f"/file/{quote(file_id, safe='')}?{urlencode(pairs)}"


def _safe_playback_second(value: str | float | int | None) -> int | None:
    """Keep workspace redirects bounded and discard malformed playback state."""
    try:
        parsed = float(value) if value not in {None, ""} else None
        second = int(parsed) if parsed is not None and math.isfinite(parsed) else None
    except (OverflowError, TypeError, ValueError):
        return None
    return min(second, 7 * 24 * 60 * 60) if second is not None and second > 0 else None


def _speaker_keys_for_editing(session, row: PlaudFile, segments: list[dict]) -> list[str]:
    """Return every stable speaker identity that can be assigned again."""
    raw_row = _canonical_raw_row(row, get_settings())
    raw_keys = speaker_keys_from_segments(raw_row.segments or []) if raw_row else []
    durable_keys = session.scalars(
        select(Speaker.key).where(Speaker.file_id == row.id).order_by(Speaker.id)
    )
    return list(
        dict.fromkeys(
            [*speaker_keys_from_segments(segments), *raw_keys, *durable_keys]
        )
    )


def _transcript_revision_reason(note: str | None) -> dict | None:
    """Parse durable speaker-edit notes into localizable presentation data."""
    if not note:
        return None
    reassigned = re.fullmatch(
        r"(?:(edited text) and )?reassigned segment (\d+) from (.+) to (.+)", note
    )
    if reassigned:
        return {
            "kind": "text_and_speaker" if reassigned.group(1) else "speaker",
            "segment": int(reassigned.group(2)) + 1,
            "from": None if reassigned.group(3) == "unassigned" else reassigned.group(3),
            "to": None if reassigned.group(4) == "unassigned" else reassigned.group(4),
        }
    normalized = re.fullmatch(r"normalized segment (\d+) word speakers as (.+)", note)
    if normalized:
        return {
            "kind": "speaker_normalized",
            "segment": int(normalized.group(1)) + 1,
            "to": None if normalized.group(2) == "unassigned" else normalized.group(2),
        }
    return None


def _library_context_title(params: dict, organization: dict) -> str:
    """Return a concise side-list label for the current library context."""
    if params["q"]:
        return "Search results"
    if params["folder"] is not None:
        folder = next(
            (item for item in organization["folders"] if item["id"] == params["folder"]),
            None,
        )
        return folder["name"] if folder is not None else "All files"
    if params["tag"] is not None:
        tag = next(
            (item for item in organization["tags"] if item["id"] == params["tag"]),
            None,
        )
        return tag["name"] if tag is not None else "All files"
    if params["view"] == "trash":
        return "Trash"
    if params["view"] == "uncategorized":
        return "Uncategorized"
    if params["origin"] == "plaud":
        return "Plaud recordings"
    if params["origin"] == "local":
        return "Local uploads"
    if any(
        params[key] is not None
        for key in (
            "state",
            "scene",
            "date_from",
            "date_to",
            "min_duration_minutes",
            "max_duration_minutes",
        )
    ):
        return "Filtered files"
    return "All files"


def _library_date_ms(value: str, timezone_name: str, *, exclusive_end: bool = False) -> int:
    """Convert a workspace-local calendar day to a UTC millisecond boundary."""
    return calendar_date_ms(value, timezone_name, exclusive_end=exclusive_end)


def _library_query(params: dict, timezone_name: str = "UTC"):
    """Build a PlaudFile select from normalized library params."""
    column = _SORT_COLUMNS[params["sort"]]
    order = column.asc() if params["dir"] == "asc" else column.desc()
    # Stable tiebreaker so equal sort keys keep a deterministic order.
    stmt = select(PlaudFile).order_by(order, PlaudFile.id.asc())
    stmt = stmt.where(PlaudFile.is_trash.is_(params["view"] == "trash"))
    if params["view"] == "uncategorized":
        stmt = stmt.where(PlaudFile.folder_id.is_(None), ~PlaudFile.tags.any())
    if params["q"]:
        pattern = f"%{_escape_like_literal(params['q'])}%"
        stmt = stmt.where(
            or_(
                PlaudFile.local_title.ilike(pattern, escape="\\"),
                PlaudFile.filename.ilike(pattern, escape="\\"),
            )
        )
    if params["state"] is not None:
        alias_states = _STATE_ALIASES.get(params["state"])
        if alias_states is not None:
            stmt = stmt.where(PlaudFile.status.in_(alias_states))
        else:
            stmt = stmt.where(PlaudFile.status == params["state"])
    if params["scene"] is not None:
        stmt = stmt.where(
            PlaudFile.scene.is_(None)
            if params["scene"] == "unknown"
            else PlaudFile.scene == params["scene"]
        )
    if params["folder"] is not None:
        stmt = stmt.where(PlaudFile.folder_id == params["folder"])
    if params["tag"] is not None:
        stmt = stmt.where(PlaudFile.tags.any(Tag.id == params["tag"]))
    if params["origin"] == "plaud":
        stmt = stmt.where(or_(PlaudFile.origin == "plaud", PlaudFile.origin.is_(None)))
    elif params["origin"] == "local":
        stmt = stmt.where(PlaudFile.origin == "local")
    if params["date_from"] is not None:
        stmt = stmt.where(
            PlaudFile.start_time_ms >= _library_date_ms(params["date_from"], timezone_name)
        )
    if params["date_to"] is not None:
        stmt = stmt.where(
            PlaudFile.start_time_ms
            < _library_date_ms(params["date_to"], timezone_name, exclusive_end=True)
        )
    if params["min_duration_minutes"] is not None:
        stmt = stmt.where(PlaudFile.duration_ms >= round(params["min_duration_minutes"] * 60_000))
    if params["max_duration_minutes"] is not None:
        stmt = stmt.where(PlaudFile.duration_ms <= round(params["max_duration_minutes"] * 60_000))
    return stmt


def _library_facets(session, params: dict) -> dict:
    """Cheap aggregate context: trash count and distinct capture-source scenes."""
    trash_count = (
        session.scalar(
            select(func.count()).select_from(PlaudFile).where(PlaudFile.is_trash.is_(True))
        )
        or 0
    )
    scene_rows = session.execute(
        select(PlaudFile.scene, func.count())
        .where(PlaudFile.is_trash.is_(False))
        .group_by(PlaudFile.scene)
        .order_by(PlaudFile.scene)
    ).all()
    scenes = [
        {
            "value": sc if sc is not None else "unknown",
            "label": _scene_label(sc),
            "count": n,
            "active": (sc if sc is not None else "unknown") == params["scene"],
        }
        for sc, n in scene_rows
    ]
    origin_rows = session.execute(
        select(func.coalesce(PlaudFile.origin, "plaud"), func.count())
        .where(PlaudFile.is_trash.is_(False))
        .group_by(func.coalesce(PlaudFile.origin, "plaud"))
        .order_by(func.coalesce(PlaudFile.origin, "plaud"))
    ).all()
    origins = [
        {
            "value": value,
            "label": "Plaud cloud" if value == "plaud" else "Local import",
            "count": count,
            "active": value == params["origin"],
        }
        for value, count in origin_rows
    ]
    return {"trash_count": trash_count, "scenes": scenes, "origins": origins}


# --------------------------------------------------------------------------- #
# health + JSON API
# --------------------------------------------------------------------------- #


class WorkspacePreferencesBody(BaseModel):
    workspace_name: str = Field(min_length=1, max_length=80)
    theme: Literal["light"] = "light"
    density: Literal["comfortable", "compact"] = "comfortable"
    timezone: str = Field(min_length=1, max_length=64)
    hour_cycle: Literal["12", "24"] = "24"
    locale: Literal["en", "zh-Hant-TW"] = "en"
    auto_process_new_recordings: bool = True

    @field_validator("workspace_name")
    @classmethod
    def clean_workspace_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Workspace name is required")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        return validate_timezone(value)


@app.get("/api/preferences/workspace")
def workspace_preferences_get():
    with session_scope() as session:
        return get_workspace_preferences(session)


@app.put("/api/preferences/workspace")
def workspace_preferences_update(body: WorkspacePreferencesBody):
    with session_scope() as session:
        return save_workspace_preferences(session, body.model_dump())


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/api/files")
def api_files(
    q: str | None = None,
    sort: str | None = None,
    dir: str | None = None,
    state: str | None = None,
    scene: str | None = None,
    view: str | None = None,
    folder: str | None = None,
    tag: str | None = None,
    origin: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_duration_minutes: str | None = None,
    max_duration_minutes: str | None = None,
) -> JSONResponse:
    params = _parse_library_params(
        q,
        sort,
        dir,
        state,
        scene,
        view,
        folder,
        tag,
        origin,
        date_from,
        date_to,
        min_duration_minutes,
        max_duration_minutes,
    )
    with session_scope() as session:
        timezone_name = get_workspace_preferences(session)["timezone"]
        rows = list(session.scalars(_library_query(params, timezone_name)))
        data = _file_summaries(session, rows)
    return JSONResponse({"files": data})


@app.get("/api/files/{file_id}/usage")
def file_usage(file_id: str) -> dict:
    from ..providers.service import resolve_recording_profile
    from ..providers.usage import cost_budget_status

    with session_scope() as session:
        if session.get(PlaudFile, file_id) is None:
            raise HTTPException(status_code=404, detail="recording not found")
        rows = list(
            session.scalars(
                select(StageAttempt)
                .where(StageAttempt.file_id == file_id)
                .order_by(StageAttempt.id)
            )
        )
        budget = cost_budget_status(
            session, file_id, resolve_recording_profile(session, file_id).to_dict()
        )
    attempts = [
        {
            "stage": row.stage.value,
            "attempt": row.attempt,
            "status": row.status.value,
            "provider": row.provider,
            "model": row.model,
            "latency_ms": row.latency_ms,
            "usage": row.usage or {},
            "fallback": (row.resolved_profile_snapshot or {}).get("fallback"),
            "estimated_cost_usd": row.estimated_cost_usd or 0,
            "started_at": row.started_at.isoformat(),
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        }
        for row in rows
    ]
    return {
        "file_id": file_id,
        "attempts": attempts,
        "totals": {
            "attempts": len(attempts),
            "latency_ms": sum(row.latency_ms or 0 for row in rows),
            "estimated_cost_usd": round(sum(row.estimated_cost_usd or 0 for row in rows), 6),
        },
        "budget": budget,
    }


@app.get("/api/files/picker")
def recording_picker(q: str = "", limit: int = 20) -> dict:
    q = q.strip()
    if len(q) > 200:
        raise HTTPException(status_code=422, detail="search must not exceed 200 characters")
    if not 1 <= limit <= 50:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 50")
    stmt = select(PlaudFile).where(PlaudFile.is_trash.is_(False))
    if q:
        pattern = f"%{_escape_like_literal(q)}%"
        stmt = stmt.where(
            or_(
                PlaudFile.local_title.ilike(pattern, escape="\\"),
                PlaudFile.filename.ilike(pattern, escape="\\"),
            )
        )
    stmt = stmt.order_by(PlaudFile.start_time_ms.desc(), PlaudFile.id.desc()).limit(limit)
    with session_scope() as session:
        rows = list(session.scalars(stmt))
        return {
            "recordings": [
                {
                    "id": row.id,
                    "title": row.display_title,
                    "recorded_at": (
                        datetime.fromtimestamp(row.start_time_ms / 1000, tz=UTC).isoformat()
                        if row.start_time_ms is not None
                        else None
                    ),
                }
                for row in rows
            ]
        }


class OrganizationItemBody(BaseModel):
    name: str
    color: str | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be empty")
        if len(value) > 80:
            raise ValueError("name must be at most 80 characters")
        return value


class OrganizeFilesBody(BaseModel):
    file_ids: list[str] = Field(min_length=1)
    folder_id: int | None = None
    add_tag_ids: list[int] = Field(default_factory=list)
    remove_tag_ids: list[int] = Field(default_factory=list)


class BulkFilesBody(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=200)
    action: Literal["resume", "delete_local_processing"]


class BulkExportBody(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=200)
    transcript_format: Literal["txt", "srt", "vtt", "docx", "pdf"] | None = None
    notes_format: Literal["md", "txt", "docx", "pdf"] | None = None
    timestamps: bool = True
    speakers: bool = True


class AskThreadTitleBody(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("conversation title must not be empty")
        if len(value) > 200:
            raise ValueError("conversation title must not exceed 200 characters")
        return value


class RecordingTitleBody(BaseModel):
    title: str | None = Field(default=None, max_length=512)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


def _organization_item(row: Folder | Tag) -> dict:
    return {"id": row.id, "name": row.name, "color": row.color}


def _require_unique_name(session, model, name: str, *, exclude_id: int | None = None) -> None:
    stmt = select(model.id).where(func.lower(model.name) == name.lower())
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    if session.scalar(stmt) is not None:
        raise HTTPException(status_code=409, detail="name already exists")


def _organization_summary(session) -> dict:
    folders = list(session.scalars(select(Folder).order_by(func.lower(Folder.name), Folder.id)))
    tags = list(session.scalars(select(Tag).order_by(func.lower(Tag.name), Tag.id)))
    folder_counts = dict(
        session.execute(
            select(PlaudFile.folder_id, func.count(PlaudFile.id))
            .where(PlaudFile.is_trash.is_(False), PlaudFile.folder_id.is_not(None))
            .group_by(PlaudFile.folder_id)
        ).all()
    )
    tag_counts = dict(
        session.execute(
            select(recording_tags.c.tag_id, func.count(recording_tags.c.file_id))
            .join(PlaudFile, PlaudFile.id == recording_tags.c.file_id)
            .where(PlaudFile.is_trash.is_(False))
            .group_by(recording_tags.c.tag_id)
        ).all()
    )
    return {
        "folders": [
            _organization_item(row)
            | {
                "count": folder_counts.get(row.id, 0),
                "execution_profile_id": row.execution_profile_id,
            }
            for row in folders
        ],
        "tags": [_organization_item(row) | {"count": tag_counts.get(row.id, 0)} for row in tags],
    }


def _named_speaker_summary(session) -> list[dict]:
    grouped: dict[str, dict] = {}
    rows = session.execute(
        select(Speaker.display_name, Speaker.file_id).where(
            Speaker.display_name.is_not(None), Speaker.display_name != ""
        )
    )
    for name, file_id in rows:
        clean = " ".join(name.split())
        if not clean:
            continue
        item = grouped.setdefault(clean.casefold(), {"name": clean, "file_ids": set()})
        item["file_ids"].add(file_id)
    return [
        {"name": item["name"], "recording_count": len(item["file_ids"])}
        for item in sorted(grouped.values(), key=lambda value: value["name"].casefold())
    ]


@app.get("/api/organization")
def api_organization() -> dict:
    with session_scope() as session:
        return _organization_summary(session)


def _create_organization_item(model, body: OrganizationItemBody) -> dict:
    with session_scope() as session:
        _require_unique_name(session, model, body.name)
        row = model(name=body.name, color=body.color)
        session.add(row)
        session.flush()
        return _organization_item(row)


def _update_organization_item(model, item_id: int, body: OrganizationItemBody) -> dict:
    with session_scope() as session:
        row = session.get(model, item_id)
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        _require_unique_name(session, model, body.name, exclude_id=item_id)
        row.name = body.name
        row.color = body.color
        session.flush()
        return _organization_item(row)


def _automation_references_organization(
    session, *, kind: str, item_id: int
) -> bool:
    def references(value) -> bool:
        try:
            return value is not None and int(value) == item_id
        except (TypeError, ValueError):
            return False

    for rule in session.scalars(select(AutomationRule)):
        trigger = rule.trigger or {}
        actions = rule.actions or {}
        if kind == "folder" and (
            references(trigger.get("folder_id"))
            or references(actions.get("folder_id"))
        ):
            return True
        if kind == "tag" and (
            references(trigger.get("tag_id"))
            or any(references(value) for value in (actions.get("add_tag_ids") or []))
        ):
            return True
    return False


@app.post("/api/folders", status_code=201)
def create_folder(body: OrganizationItemBody) -> dict:
    return _create_organization_item(Folder, body)


@app.patch("/api/folders/{item_id}")
def update_folder(item_id: int, body: OrganizationItemBody) -> dict:
    return _update_organization_item(Folder, item_id, body)


@app.delete("/api/folders/{item_id}")
def delete_folder(item_id: int) -> dict:
    with session_scope() as session:
        row = session.get(Folder, item_id)
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        if _automation_references_organization(
            session, kind="folder", item_id=item_id
        ):
            raise HTTPException(
                status_code=409, detail="folder is used by an AutoFlow rule"
            )
        session.execute(
            update(PlaudFile).where(PlaudFile.folder_id == item_id).values(folder_id=None)
        )
        session.delete(row)
    return {"deleted": True}


@app.post("/api/tags", status_code=201)
def create_tag(body: OrganizationItemBody) -> dict:
    return _create_organization_item(Tag, body)


@app.patch("/api/tags/{item_id}")
def update_tag(item_id: int, body: OrganizationItemBody) -> dict:
    return _update_organization_item(Tag, item_id, body)


@app.delete("/api/tags/{item_id}")
def delete_tag(item_id: int) -> dict:
    with session_scope() as session:
        row = session.get(Tag, item_id)
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        if _automation_references_organization(session, kind="tag", item_id=item_id):
            raise HTTPException(
                status_code=409, detail="tag is used by an AutoFlow rule"
            )
        session.execute(delete(recording_tags).where(recording_tags.c.tag_id == item_id))
        session.delete(row)
    return {"deleted": True}


@app.post("/api/files/organize")
def organize_files(body: OrganizeFilesBody) -> dict:
    folder_requested = "folder_id" in body.model_fields_set
    if not folder_requested and not body.add_tag_ids and not body.remove_tag_ids:
        raise HTTPException(status_code=422, detail="at least one mutation is required")
    file_ids = list(dict.fromkeys(body.file_ids))
    add_ids = set(body.add_tag_ids)
    remove_ids = set(body.remove_tag_ids)
    with session_scope() as session:
        files = list(session.scalars(select(PlaudFile).where(PlaudFile.id.in_(file_ids))))
        if {row.id for row in files} != set(file_ids):
            raise HTTPException(status_code=404, detail="one or more files were not found")
        folder = None
        if folder_requested and body.folder_id is not None:
            folder = session.get(Folder, body.folder_id)
            if folder is None:
                raise HTTPException(status_code=404, detail="folder not found")
        requested_tag_ids = add_ids | remove_ids
        tags = (
            list(session.scalars(select(Tag).where(Tag.id.in_(requested_tag_ids))))
            if requested_tag_ids
            else []
        )
        if {tag.id for tag in tags} != requested_tag_ids:
            raise HTTPException(status_code=404, detail="one or more tags were not found")
        tags_by_id = {tag.id: tag for tag in tags}
        for row in files:
            if folder_requested:
                row.folder = folder
            if remove_ids:
                row.tags = [tag for tag in row.tags if tag.id not in remove_ids]
            existing = {tag.id for tag in row.tags}
            row.tags.extend(tags_by_id[tag_id] for tag_id in sorted(add_ids - existing))
    return {"updated": len(files)}


@app.patch("/api/files/{file_id}/title")
def update_recording_title(file_id: str, body: RecordingTitleBody) -> dict:
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            raise HTTPException(status_code=404, detail="recording not found")
        row.local_title = body.title
        session.flush()
        return {
            "file_id": row.id,
            "title": row.display_title,
            "local_title": row.local_title,
            "cloud_title": row.filename,
        }


@app.delete("/api/files/{file_id}/local-audio")
def delete_recording_local_audio(file_id: str) -> dict:
    from ..local_cleanup import remove_local_audio

    try:
        return remove_local_audio(file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/files/{file_id}/local-processing")
def delete_recording_local_processing(file_id: str) -> dict:
    from ..local_cleanup import delete_local_processing

    try:
        return delete_local_processing(file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/files/bulk")
def bulk_files(body: BulkFilesBody) -> dict:
    from ..worker.pipeline import processing_claim_active, reset_pipeline_retry

    file_ids = list(dict.fromkeys(body.file_ids))
    if body.action == "delete_local_processing":
        from ..local_cleanup import delete_local_processing_many

        try:
            result = delete_local_processing_many(file_ids)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"action": body.action, "updated": len(file_ids), **result}

    from datetime import datetime

    now = datetime.now(UTC)
    with session_scope() as session:
        rows = list(session.scalars(select(PlaudFile).where(PlaudFile.id.in_(file_ids))))
        if {row.id for row in rows} != set(file_ids):
            raise HTTPException(status_code=404, detail="recording not found")
        if any(not row.audio_path for row in rows):
            raise HTTPException(status_code=409, detail="a selected recording has no local audio")
        if any(processing_claim_active(row) for row in rows):
            raise HTTPException(status_code=409, detail="a selected recording is processing")
        for row in rows:
            reset_pipeline_retry(row)
            row.status = FileStatus.partial if row.local_transcript else FileStatus.error
            row.error = "Queued by bulk Resume."
            row.pipeline_last_failure_at = now
            row.pipeline_next_retry_at = now
    return {"action": body.action, "updated": len(file_ids), "queued": file_ids}


@app.post("/api/files/export")
def bulk_export_files(body: BulkExportBody) -> StreamingResponse:
    from ..bulk_export import (
        BulkExportRequest,
        BulkExportValidationError,
        NoExportableContentError,
        UnknownRecordingIdsError,
        build_bulk_export,
    )

    try:
        result = build_bulk_export(
            BulkExportRequest(
                recording_ids=body.file_ids,
                transcript_format=body.transcript_format,
                notes_format=body.notes_format,
                timestamps=body.timestamps,
                speakers=body.speakers,
            )
        )
    except BulkExportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UnknownRecordingIdsError as exc:
        raise HTTPException(
            status_code=404,
            detail="one or more selected recordings no longer exists",
        ) from exc
    except NoExportableContentError as exc:
        raise HTTPException(
            status_code=409,
            detail="none of the selected recordings has an available transcript or note",
        ) from exc

    statuses = [
        output["status"]
        for recording in result.manifest["recordings"]
        for output in recording["outputs"]
    ]

    def chunks():
        try:
            yield from result
        finally:
            result.close()

    headers = {
        "Content-Disposition": f'attachment; filename="{result.filename}"',
        "Content-Length": str(result.size_bytes),
        "Cache-Control": "no-store",
        "X-Localplaud-Export-Emitted": str(statuses.count("emitted")),
        "X-Localplaud-Export-Skipped": str(statuses.count("skipped")),
        "X-Localplaud-Export-Errors": str(statuses.count("error")),
    }
    return StreamingResponse(
        chunks(),
        media_type=result.media_type,
        headers=headers,
        background=BackgroundTask(result.close),
    )


@app.get("/api/ask/threads")
def ask_thread_history(
    file_id: str | None = None,
    q: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    from ..ask_threads import list_threads

    try:
        return list_threads(file_id, query=q, page=page, page_size=page_size)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.patch("/api/ask/threads/{thread_id}")
def rename_ask_thread(
    thread_id: str,
    body: AskThreadTitleBody,
    file_id: str | None = None,
) -> dict:
    from ..ask_threads import rename_thread

    try:
        return rename_thread(thread_id, body.title, file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/ask/threads/{thread_id}")
def delete_ask_thread(thread_id: str, file_id: str | None = None) -> dict:
    from ..ask_threads import delete_thread

    try:
        return delete_thread(thread_id, file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #


def _wants_progressive_shell(request: Request) -> bool:
    """Use a fast shell for real browser navigation while preserving full SSR fallback."""
    if request.headers.get("hx-history-restore-request", "").lower() == "true":
        return False
    return (
        request.headers.get("sec-fetch-dest", "").lower() == "document"
        or (
            request.headers.get("hx-request", "").lower() == "true"
            and request.headers.get("hx-target") == "app-view"
        )
    )


def _stats(session) -> dict:
    # Every count shares the Library's non-trash visibility so a tile's number
    # always equals its linked destination's rows.
    visible = PlaudFile.is_trash.is_(False)
    total = session.scalar(select(func.count()).select_from(PlaudFile).where(visible)) or 0
    done = (
        session.scalar(
            select(func.count())
            .select_from(PlaudFile)
            .where(visible, PlaudFile.status == FileStatus.done)
        )
        or 0
    )
    processing = (
        session.scalar(
            select(func.count())
            .select_from(PlaudFile)
            # Same pending bucket as the Workspace-status card and the
            # state=generating alias.
            .where(visible, PlaudFile.status.in_(_STATE_ALIASES["generating"]))
        )
        or 0
    )
    total_ms = (
        session.scalar(
            select(func.coalesce(func.sum(PlaudFile.duration_ms), 0)).where(visible)
        )
        or 0
    )
    return {
        "total": total,
        "done": done,
        "processing": processing,
        "hours": round(total_ms / 3_600_000, 1),
    }


@app.get("/home", response_class=HTMLResponse)
def home(request: Request):
    with session_scope() as session:
        recent_rows = list(
            session.scalars(
                select(PlaudFile)
                .where(PlaudFile.is_trash.is_(False))
                .order_by(PlaudFile.start_time_ms.desc().nullslast(), PlaudFile.id)
                .limit(12)
            )
        )
        attention_rows = list(
            session.scalars(
                select(PlaudFile)
                .where(
                    PlaudFile.is_trash.is_(False),
                    PlaudFile.status.in_([FileStatus.error, FileStatus.partial]),
                )
                .order_by(PlaudFile.updated_at.desc())
                .limit(6)
            )
        )
        metadata_only = (
            session.scalar(
                select(func.count())
                .select_from(PlaudFile)
                .where(PlaudFile.status == FileStatus.metadata_only)
            )
            or 0
        )
        audio_local = (
            session.scalar(
                select(func.count()).select_from(PlaudFile).where(PlaudFile.audio_path.is_not(None))
            )
            or 0
        )
        import_run = session.scalar(
            select(ImportRun).order_by(ImportRun.created_at.desc()).limit(1)
        )
        automation_run = session.scalar(
            select(AutomationRun).order_by(AutomationRun.created_at.desc()).limit(1)
        )
        automation_count = session.scalar(select(func.count()).select_from(AutomationRun)) or 0
        stats = _stats(session)
        recent_files = _file_summaries(session, recent_rows)
        attention_files = _file_summaries(session, attention_rows)
    ctx = _base_ctx(request, "home") | {
        "recent_files": recent_files,
        "attention_files": attention_files,
        "stats": stats,
        "metadata_only": metadata_only,
        "audio_local": audio_local,
        "import_run": (
            {
                "status": import_run.status,
                "processed": import_run.processed,
                "total": import_run.total,
                "transcripts": import_run.transcript_count,
                "summaries": import_run.summary_count,
                "failed": import_run.failed_count,
            }
            if import_run
            else None
        ),
        "automation_count": automation_count,
        "automation_run": (
            {
                "status": automation_run.status,
                "file_id": automation_run.file_id,
                "created_at": automation_run.created_at.strftime("%b %d · %H:%M"),
            }
            if automation_run
            else None
        ),
    }
    return templates.TemplateResponse(request=request, name="home.html", context=ctx)


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str | None = None,
    sort: str | None = None,
    dir: str | None = None,
    state: str | None = None,
    scene: str | None = None,
    view: str | None = None,
    folder: str | None = None,
    tag: str | None = None,
    ask_thread: str | None = None,
    origin: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_duration_minutes: str | None = None,
    max_duration_minutes: str | None = None,
    page: int = 1,
    workspace: bool = False,
    preserve_filelist: bool = False,
):
    active_page = (
        "ask"
        if request.query_params.get("ask") == "true" or ask_thread is not None
        else "recordings"
    )
    if not workspace and _wants_progressive_shell(request):
        keep_filelist = (
            request.headers.get("x-localplaud-preserve-filelist", "").lower() == "true"
        )
        workspace_url = request.url.include_query_params(workspace="true")
        if keep_filelist:
            workspace_url = workspace_url.include_query_params(preserve_filelist="true")
        return templates.TemplateResponse(
            request=request,
            name="index_loading.html",
            context=_base_ctx(request, active_page)
            | {
                "workspace_url": str(workspace_url),
                "preserve_filelist": keep_filelist,
            },
        )
    params = _parse_library_params(
        q,
        sort,
        dir,
        state,
        scene,
        view,
        folder,
        tag,
        origin,
        date_from,
        date_to,
        min_duration_minutes,
        max_duration_minutes,
    )
    with session_scope() as session:
        timezone_name = get_workspace_preferences(session)["timezone"]
        library_query = _library_query(params, timezone_name)
        total = (
            session.scalar(
                select(func.count()).select_from(library_query.order_by(None).subquery())
            )
            or 0
        )
        page_size = 100
        page_count = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, page_count))
        rows = list(
            session.scalars(
                library_query
                if active_page == "ask"
                else library_query.offset((page - 1) * page_size).limit(page_size)
            )
        )
        files = _file_summaries(session, rows)
        stats = _stats(session)
        facets = _library_facets(session, params)
        organization = _organization_summary(session)
        named_speakers = _named_speaker_summary(session)
        recent_ask_threads = list(
            session.scalars(
                select(AskThread)
                .where(AskThread.file_id.is_(None))
                .order_by(AskThread.updated_at.desc())
                .limit(5)
            )
        )
        selected_ask_thread = session.get(AskThread, ask_thread) if ask_thread else None
        if selected_ask_thread is not None and selected_ask_thread.file_id is not None:
            selected_ask_thread = None
        selected_ask_thread_data = (
            thread_to_dict(selected_ask_thread) if selected_ask_thread is not None else None
        )
    ctx = _base_ctx(request, active_page) | {
        "files": files,
        "stats": stats,
        "q": params["q"],
        "lib": params,
        "facets": facets,
        "organization": organization,
        "states": [s.value for s in FileStatus],
        "attention_states": _ATTENTION_STATES,
        "ask_threads": [{"id": row.id, "title": row.title} for row in recent_ask_threads],
        "selected_ask_thread": selected_ask_thread_data,
        "ask_skills": list_ask_skills("library"),
        "named_speakers": named_speakers,
        "pagination": {
            "page": page,
            "pages": page_count,
            "page_size": page_size,
            "total": total,
        },
        "preserve_filelist": preserve_filelist or not workspace,
        "return_to": _library_return_url(request),
        "return_to_param": quote(_library_return_url(request), safe=""),
    }
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@app.get("/search", response_class=HTMLResponse)
def search(
    request: Request,
    q: str | None = None,
    folder: str | None = None,
    tag: str | None = None,
    origin: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
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
                stmt = stmt.where(
                    or_(PlaudFile.origin == "plaud", PlaudFile.origin.is_(None))
                )
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
    with session_scope() as session:
        organization = _organization_summary(session)
    ctx = _base_ctx(request, "search") | {
        "q": q or "",
        "groups": groups,
        "organization": organization,
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
    return templates.TemplateResponse(request=request, name="search.html", context=ctx)


@app.get("/templates", response_class=HTMLResponse)
def template_library(
    request: Request,
    tab: str = "my",
    q: str = "",
    category: str | None = None,
):
    tab = tab if tab in {"my", "explore"} else "my"
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(NoteTemplate)
                .where(NoteTemplate.is_active.is_(True))
                .order_by(NoteTemplate.is_builtin.desc(), NoteTemplate.name)
            )
        )
        items = [note_template_item(row) for row in rows]
    if tab == "explore":
        items = [item for item in items if item["is_builtin"]]
    query = q.strip().casefold()
    if query:
        items = [
            item
            for item in items
            if query
            in " ".join(
                [item["name"], item["description"], item["category"], item["scenario"]]
            ).casefold()
        ]
    categories = sorted({item["category"] for item in items})
    if category:
        items = [item for item in items if item["category"] == category]
    ctx = _base_ctx(request, "templates") | {
        "tab": tab,
        "q": q,
        "category": category,
        "categories": categories,
        "template_items": items,
    }
    return templates.TemplateResponse(request=request, name="templates.html", context=ctx)


@app.get("/discover", response_class=HTMLResponse)
def discover_automations(request: Request):
    from ..email_integrations import list_email_integrations
    from ..integrations import list_webhook_integrations
    from .automations import list_rules, list_runs

    with session_scope() as session:
        organization = _organization_summary(session)
        profiles = [
            {"id": row.id, "name": row.name, "version": row.version}
            for row in session.scalars(
                select(ExecutionProfile).order_by(
                    ExecutionProfile.name, ExecutionProfile.version.desc()
                )
            )
        ]
        note_templates = [
            {"key": row.key, "name": row.name, "version": row.version}
            for row in session.scalars(
                select(NoteTemplate)
                .where(NoteTemplate.is_active.is_(True))
                .order_by(NoteTemplate.name)
            )
        ]
        webhook_integrations = [
            item for item in list_webhook_integrations(session) if item["enabled"]
        ]
        email_integrations = [
            item for item in list_email_integrations(session) if item["enabled"]
        ]
    automation_rules = list_rules()["rules"]
    ctx = _base_ctx(request, "discover") | {
        "automation_rules": automation_rules,
        "automation_runs": list_runs(limit=50)["runs"],
        "organization": organization,
        "profiles": profiles,
        "note_templates": note_templates,
        "webhook_integrations": webhook_integrations,
        "email_integrations": email_integrations,
        "application_catalog": [
            {
                "name": "Local AutoFlow",
                "detail": "Rules created and fully editable in this Web App.",
                "count": sum(1 for rule in automation_rules if rule["editable"]),
                "status": "available",
            },
            {
                "name": "External rule owners",
                "detail": "Mirrored rules stay visible but can only be edited by their owner.",
                "count": sum(1 for rule in automation_rules if not rule["editable"]),
                "status": "connected" if any(not rule["editable"] for rule in automation_rules) else "idle",
            },
            {
                "name": "Authorized webhooks",
                "detail": "Scoped HTTPS or explicitly allowed private destinations.",
                "count": len(webhook_integrations),
                "status": "configured" if webhook_integrations else "setup",
                "href": "/settings#webhook-integrations",
            },
            {
                "name": "Authorized email",
                "detail": "Scoped SMTP destinations with environment-only passwords.",
                "count": len(email_integrations),
                "status": "configured" if email_integrations else "setup",
                "href": "/settings#email-integrations",
            },
        ],
    }
    return templates.TemplateResponse(request=request, name="discover.html", context=ctx)


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    from .automations import list_notifications

    rows = list_notifications(limit=200)["notifications"]
    return templates.TemplateResponse(
        request=request,
        name="notifications.html",
        context=_base_ctx(request, "notifications") | {"notifications": rows},
    )


_AUDIO_MIME = {"mp3": "audio/mpeg", "opus": "audio/ogg", "wav": "audio/wav", "m4a": "audio/mp4"}
_waveform_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="localplaud-waveform")
_waveform_jobs: dict[tuple[str, int, int, int], Future[list[float]]] = {}
_waveform_jobs_lock = Lock()


@app.get("/audio/{file_id}")
def audio(file_id: str):
    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        path = r.audio_path if r else None
    if not path or not Path(path).exists():
        return JSONResponse({"error": "audio not downloaded"}, status_code=404)
    ext = Path(path).suffix.lstrip(".").lower()
    return FileResponse(path, media_type=_AUDIO_MIME.get(ext, "application/octet-stream"))


@app.get("/audio/{file_id}/waveform")
def audio_waveform(file_id: str, buckets: int = 180):
    import subprocess

    from ..waveform import cached_waveform_peaks, waveform_peaks

    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        path = row.audio_path if row else None
    if not path or not Path(path).exists():
        raise HTTPException(status_code=409, detail="recording audio has not been imported")
    buckets = min(max(int(buckets), 32), 500)
    try:
        peaks = cached_waveform_peaks(path, buckets=buckets)
    except (subprocess.SubprocessError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"could not build waveform: {exc}") from exc
    if peaks is not None:
        return {"file_id": file_id, "buckets": len(peaks), "peaks": peaks}
    stat = Path(path).stat()
    job_key = (str(path), buckets, stat.st_size, stat.st_mtime_ns)
    with _waveform_jobs_lock:
        future = _waveform_jobs.get(job_key)
        if future is None:
            future = _waveform_executor.submit(waveform_peaks, path, buckets=buckets)
            _waveform_jobs[job_key] = future
    if not future.done():
        return JSONResponse(
            {"file_id": file_id, "status": "processing"},
            status_code=202,
            headers={"Retry-After": "1"},
        )
    try:
        peaks = future.result()
    except (subprocess.SubprocessError, ValueError) as exc:
        with _waveform_jobs_lock:
            _waveform_jobs.pop(job_key, None)
        raise HTTPException(status_code=500, detail=f"could not build waveform: {exc}") from exc
    with _waveform_jobs_lock:
        _waveform_jobs.pop(job_key, None)
    return {"file_id": file_id, "buckets": len(peaks), "peaks": peaks}


@app.get("/api/files/{file_id}/acceptance")
def recording_acceptance(file_id: str) -> dict:
    from ..acceptance import subscription_independence_report

    try:
        return subscription_independence_report(file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/file/{file_id}/acceptance-panel", response_class=HTMLResponse)
def recording_acceptance_panel(request: Request, file_id: str):
    from ..acceptance import subscription_independence_report

    try:
        acceptance = subscription_independence_report(file_id)
    except LookupError:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(
        request=request,
        name="_acceptance_panel.html",
        context=_base_ctx(request, "recordings")
        | {"file_id": file_id, "acceptance": acceptance},
    )


def _canonical_raw_row(r: PlaudFile, settings) -> Transcript | None:
    """The raw transcript selected by configured provenance rules."""
    if settings.pipeline.artifact_mode == "independent":
        return r.local_transcript
    if settings.pipeline.prefer_cloud_artifacts:
        return r.plaud_transcript or r.local_transcript
    return r.local_transcript


def _canonical_revision(r: PlaudFile, raw_row: Transcript | None):
    """Latest correction in the selected raw transcript's provenance lane."""
    source = raw_row.source if raw_row is not None else "local"
    return r.corrected_transcript_for_source(source)


def _transcript_page_token(row: Transcript) -> str:
    payload = json.dumps(
        {
            "created_at": row.created_at.isoformat(),
            "segments": row.segments or [],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _recording_edit_blocked(row: PlaudFile) -> bool:
    """Derived generation owns a stable input snapshot while its lease is active."""
    from ..worker.pipeline import processing_claim_active

    return processing_claim_active(row)


def _serialize_transcript_mutation(session) -> None:
    """Linearize revision reads/writes on SQLite before stale-write checks."""
    if session.get_bind().dialect.name == "sqlite":
        session.execute(sql_text("BEGIN IMMEDIATE"))


def _mark_derived_stale(session, file_id: str, stages: tuple[StageName, ...]) -> None:
    """Preserve derived rows but make stale artifacts ineligible for reuse/UI."""
    stale_generation = secrets.token_hex(16)
    for stage in stages:
        run = session.scalar(
            select(StageRun).where(StageRun.file_id == file_id, StageRun.stage == stage)
        )
        if run is None:
            run = StageRun(file_id=file_id, stage=stage, attempts=0, detail={})
            session.add(run)
        run.status = StageStatus.pending
        run.error = None
        run.completed_at = None
        run.detail = dict(run.detail or {}) | {
            "stale": True,
            "stale_generation": stale_generation,
        }


@app.get("/file/{file_id}/transcript-page", response_class=HTMLResponse)
def recording_transcript_page(
    request: Request,
    file_id: str,
    source: str = "canonical",
    view: str = "corrected",
    revision: int | None = None,
    page_revision: int | None = None,
    page_transcript_id: int | None = None,
    page_transcript_token: str | None = None,
    offset: int = 0,
    limit: int = 120,
    return_to: str | None = None,
    tab: str = "transcript",
    t: str | None = None,
):
    settings = get_settings()
    offset = max(0, offset)
    limit = min(max(20, limit), 200)
    if source not in {"canonical", "imported"}:
        return HTMLResponse("Unknown transcript source", status_code=422)
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            return HTMLResponse("Not found", status_code=404)
        raw_row = _canonical_raw_row(row, settings)
        corrected = _canonical_revision(row, raw_row)
        selected_revision = None
        pinned_transcript_id = None
        pinned_transcript_token = None
        can_edit = False
        transcript_revision = None
        if source == "imported":
            if offset > 0 and (
                page_transcript_id is None or page_transcript_token is None
            ):
                return HTMLResponse("Transcript version is required", status_code=409)
            transcript_row = (
                session.get(Transcript, page_transcript_id)
                if page_transcript_id is not None
                else row.plaud_transcript
            )
            if transcript_row is not None and (
                transcript_row.file_id != file_id
                or transcript_row.source not in {"cloud", "plaud"}
            ):
                return HTMLResponse("Transcript version not found", status_code=404)
            if page_transcript_id is not None and transcript_row is None:
                return HTMLResponse("Transcript version not found", status_code=404)
            if (
                transcript_row is not None
                and page_transcript_token is not None
                and _transcript_page_token(transcript_row) != page_transcript_token
            ):
                return HTMLResponse("Transcript version not found", status_code=404)
            segments = list(transcript_row.segments or []) if transcript_row else []
            pinned_transcript_id = transcript_row.id if transcript_row is not None else None
            pinned_transcript_token = (
                _transcript_page_token(transcript_row) if transcript_row is not None else None
            )
        elif raw_row is None:
            if offset > 0:
                return HTMLResponse("Transcript version is required", status_code=409)
            if page_transcript_id is not None:
                return HTMLResponse("Transcript version not found", status_code=404)
            segments = []
        elif view == "raw" or corrected is None:
            if offset > 0 and (
                page_transcript_id is None or page_transcript_token is None
            ):
                return HTMLResponse("Transcript version is required", status_code=409)
            transcript_row = (
                session.get(Transcript, page_transcript_id)
                if page_transcript_id is not None
                else raw_row
            )
            if transcript_row is None or (
                transcript_row.file_id != file_id or transcript_row.source != raw_row.source
            ):
                return HTMLResponse("Transcript version not found", status_code=404)
            if (
                page_transcript_token is not None
                and _transcript_page_token(transcript_row) != page_transcript_token
            ):
                return HTMLResponse("Transcript version not found", status_code=404)
            segments = list(transcript_row.segments or [])
            pinned_transcript_id = transcript_row.id
            pinned_transcript_token = _transcript_page_token(transcript_row)
            can_edit = corrected is None
        else:
            if offset > 0 and revision is None and page_revision is None:
                return HTMLResponse("Transcript revision is required", status_code=409)
            requested_revision = revision if revision is not None else page_revision
            selected_revision = next(
                (
                    item
                    for item in row.transcript_revisions
                    if item.source == raw_row.source and item.revision == requested_revision
                ),
                None,
            )
            if requested_revision is not None and selected_revision is None:
                return HTMLResponse("Transcript revision not found", status_code=404)
            selected_revision = selected_revision or corrected
            segments = list(selected_revision.segments or [])
            transcript_revision = selected_revision.revision
            can_edit = revision is None
        page_segments = segments[offset : offset + limit]
        next_offset = offset + limit if offset + limit < len(segments) else None
        speaker_names = display_names(session, file_id)
        speaker_options = [
            {"key": key, "name": speaker_names.get(key) or key}
            for key in _speaker_keys_for_editing(session, row, segments)
        ]
    return_to = _validated_library_return_url(return_to)
    tab = tab if tab in {"transcript", "notes", "mindmap", "ask"} else "transcript"
    playback_second = _safe_playback_second(t)
    return templates.TemplateResponse(
        request=request,
        name="_transcript_page.html",
        context=_base_ctx(request, "recordings")
        | {
            "file_id": file_id,
            "source": source,
            "view": view,
            "revision": revision,
            "page_revision": (
                selected_revision.revision
                if selected_revision is not None and revision is None
                else None
            ),
            "page_transcript_id": pinned_transcript_id,
            "page_transcript_token": pinned_transcript_token,
            "offset": offset,
            "limit": limit,
            "segments": page_segments,
            "next_offset": next_offset,
            "speaker_names": speaker_names,
            "speaker_options": speaker_options,
            "can_edit": can_edit and source != "imported",
            "transcript_revision": transcript_revision,
            "return_to": return_to,
            "tab": tab,
            "playback_second": playback_second,
        },
    )


def _lineage_label(input_transcript_revision: int | None) -> str:
    if input_transcript_revision == 0:
        return "raw ASR"
    if input_transcript_revision is not None:
        return f"transcript rev {input_transcript_revision}"
    return "legacy / unknown transcript"


def _fmt_history_time(value: datetime | None) -> str | None:
    return value.strftime("%b %d, %Y · %H:%M") if value else None


_NOTE_HISTORY_PREVIEW_LIMIT = 20


def _note_history_entries(
    rows: list[SummaryRevision],
    live_fingerprint: str,
    limit: int = _NOTE_HISTORY_PREVIEW_LIMIT,
) -> list[dict]:
    """Newest-first archived versions of one generated output, bounded for UI."""
    return [
        {
            "revision": item.revision,
            "title": item.title,
            "content_md": item.content_md,
            "template_version": item.template_version,
            "llm_provider": item.llm_provider,
            "model": item.model,
            "created_at": _fmt_history_time(item.created_at),
            "archived_at": _fmt_history_time(item.archived_at),
            "archive_reason": item.archive_reason,
            "lineage_label": _lineage_label(item.input_transcript_revision),
            "input_transcript_source": item.input_transcript_source,
            "is_current": content_fingerprint(item) == live_fingerprint,
        }
        for item in rows[:limit]
    ]


@app.get("/file/{file_id}", response_class=HTMLResponse)
def file_detail(
    request: Request,
    file_id: str,
    view: str | None = None,
    tab: str | None = None,
    ask_thread: str | None = None,
    revision: int | None = None,
    note_id: int | None = None,
    return_to: str | None = None,
    workspace: bool = False,
    preserve_filelist: bool = False,
    t: str | None = None,
    segment_error: str | None = None,
):
    settings = get_settings()
    return_to = _validated_library_return_url(return_to)
    return_to_param = quote(return_to, safe="")
    filelist_params, filelist_page = _library_state_from_return_url(return_to)
    playback_second = _safe_playback_second(t)
    segment_edit_error = {
        "stale_revision": "Transcript changed. Reload before saving.",
        "recording_processing": "Recording is processing. Wait until it finishes before editing.",
        "not_found": "Recording not found.",
        "no_transcript": "No transcript is available to edit.",
        "segment_not_found": "That transcript segment is no longer available.",
        "unknown_speaker": "That speaker is no longer available.",
    }.get(segment_error or "")
    active_tab = (
        "notes"
        if note_id is not None
        else "ask"
        if ask_thread is not None
        else tab
        if tab in {"transcript", "notes", "mindmap", "ask"}
        else "transcript"
    )
    if not workspace and _wants_progressive_shell(request):
        keep_filelist = (
            request.headers.get("x-localplaud-preserve-filelist", "").lower() == "true"
        )
        with session_scope() as session:
            row = session.get(PlaudFile, file_id)
            if row is None:
                return HTMLResponse("Not found", status_code=404)
            shell_file = {
                "id": row.id,
                "filename": row.display_title,
                "status": row.status.value,
                "duration_ms": row.duration_ms,
                "start_time_ms": row.start_time_ms,
            }
        workspace_url = request.url.include_query_params(workspace="true")
        if keep_filelist:
            workspace_url = workspace_url.include_query_params(preserve_filelist="true")
        return templates.TemplateResponse(
            request=request,
            name="detail_loading.html",
            context=_base_ctx(request, "recordings")
            | {
                "f": shell_file,
                "workspace_url": str(workspace_url),
                "preserve_filelist": keep_filelist,
                "return_to": return_to,
                "return_to_param": return_to_param,
            },
        )
    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return HTMLResponse("Not found", status_code=404)
        # Default template first, then the rest.
        stale_stages = {run.stage for run in r.stage_runs if (run.detail or {}).get("stale")}
        # A locally generated mind map hidden by staleness is out of date, not
        # missing — the mind-map tab reports that truthfully.
        mind_map_out_of_date = StageName.mind_map in stale_stages and any(
            s.template == "mind_map" and s.source == "local" for s in r.summaries
        )
        # A mind-map-only rebuild is offered only when its inputs are current:
        # the canonical transcript exists and the source notes are not stale.
        from ..worker.pipeline import mind_map_rebuild_source, processing_claim_active

        mind_map_rebuild_available = (
            mind_map_out_of_date
            and StageName.summarize not in stale_stages
            and r.local_transcript is not None
            and mind_map_rebuild_source(session, r, settings) is not None
        )
        mind_map_run = next(
            (run for run in r.stage_runs if run.stage == StageName.mind_map), None
        )
        mind_map_only_scope = bool(
            mind_map_run and (mind_map_run.detail or {}).get("mind_map_only")
        )
        mind_map_rebuild_in_progress = bool(
            mind_map_out_of_date
            and mind_map_only_scope
            and mind_map_run.status in {StageStatus.pending, StageStatus.running}
            and processing_claim_active(r)
        )
        mind_map_rebuild_error = (
            sanitize_error(mind_map_run.error, max_length=500)
            if mind_map_out_of_date
            and mind_map_run is not None
            and mind_map_only_scope
            and mind_map_run.status == StageStatus.failed
            and mind_map_run.error
            else None
        )
        # History is bounded at the query: per template, only the newest
        # _NOTE_HISTORY_PREVIEW_LIMIT archived bodies are loaded; totals come
        # from a count, never from materializing every version.
        history_counts = dict(
            session.execute(
                select(SummaryRevision.template, func.count())
                .where(SummaryRevision.file_id == file_id)
                .group_by(SummaryRevision.template)
            ).all()
        )
        history_rank = (
            func.row_number()
            .over(
                partition_by=SummaryRevision.template,
                order_by=SummaryRevision.revision.desc(),
            )
            .label("history_rank")
        )
        ranked_history = (
            select(SummaryRevision, history_rank)
            .where(SummaryRevision.file_id == file_id)
            .subquery()
        )
        recent_revision = aliased(SummaryRevision, ranked_history)
        history_by_template: dict[str, list[SummaryRevision]] = {}
        for item in session.scalars(
            select(recent_revision)
            .where(ranked_history.c.history_rank <= _NOTE_HISTORY_PREVIEW_LIMIT)
            .order_by(recent_revision.template, recent_revision.revision.desc())
        ):
            history_by_template.setdefault(item.template, []).append(item)
        summaries = sorted(
            [
                {
                    "id": s.id,
                    "title": s.title,
                    "content_md": s.content_md,
                    "template": s.template,
                    "template_name": (s.template_snapshot or {}).get("name")
                    or s.template.replace("-", " ").title(),
                    "template_version": s.template_version,
                    "created_at": _fmt_history_time(s.created_at),
                    "source": s.source,
                    "input_transcript_revision": s.input_transcript_revision,
                    "input_transcript_source": s.input_transcript_source,
                    "lineage_label": _lineage_label(s.input_transcript_revision),
                    "restored_from_revision": s.restored_from_revision,
                    "version_count": history_counts.get(s.template, 0)
                    if s.source == "local"
                    else 0,
                    "versions": (
                        _note_history_entries(
                            history_by_template.get(s.template, []), content_fingerprint(s)
                        )
                        if s.source == "local"
                        else []
                    ),
                }
                for s in r.summaries
                if not (
                    s.source == "local"
                    and (
                        (s.template == "mind_map" and StageName.mind_map in stale_stages)
                        or (s.template != "mind_map" and StageName.summarize in stale_stages)
                    )
                )
            ],
            key=lambda s: (s["template"] != "default", s["template"]),
        )
        transcript = None
        imported_transcript = None
        raw_row = _canonical_raw_row(r, settings)
        corrected = _canonical_revision(r, raw_row)
        revision_rows = [
            row
            for row in r.transcript_revisions
            if raw_row is not None and row.source == raw_row.source
        ]
        attempt_rows = list(
            session.scalars(
                select(StageAttempt)
                .where(StageAttempt.file_id == file_id)
                .order_by(StageAttempt.id.desc())
                .limit(50)
            )
        )
        preview_revision = next((row for row in revision_rows if row.revision == revision), None)
        # Canonical segments (latest correction wins) drive the speaker legend.
        canonical_segments = (
            corrected.segments
            if corrected is not None
            else (raw_row.segments if raw_row is not None else [])
        )
        speaker_names = display_names(session, r.id)
        speakers = [
            {"key": key, "name": speaker_names.get(key)}
            for key in _speaker_keys_for_editing(session, r, canonical_segments)
        ]
        show_corrected = corrected is not None and view != "raw"
        shown_revision = preview_revision if preview_revision is not None else corrected
        if show_corrected and shown_revision is not None:
            base = (
                session.get(Transcript, shown_revision.base_transcript_id)
                if shown_revision.base_transcript_id is not None
                else raw_row
            )
            transcript = {
                "provider": shown_revision.provider
                or (base.provider if base is not None else "local-edit"),
                "model": shown_revision.model,
                "language": base.language if base is not None else None,
                "source": "local",
                "segments": shown_revision.segments,
                "kind": "history" if preview_revision is not None else "corrected",
                "revision_kind": shown_revision.kind,
                "revision": shown_revision.revision,
            }
        elif raw_row is not None:
            transcript = {
                "provider": raw_row.provider,
                "language": raw_row.language,
                "source": raw_row.source,
                "segments": raw_row.segments,
                "kind": "raw ASR",
                "revision": None,
            }
        imported_row = r.plaud_transcript
        if imported_row is not None and imported_row is not raw_row:
            imported_transcript = {
                "provider": imported_row.provider,
                "language": imported_row.language,
                "source": imported_row.source,
                "segments": imported_row.segments,
            }
        f = {
            "id": r.id,
            "filename": r.display_title,
            "cloud_filename": r.filename,
            "local_title": r.local_title,
            "status": r.status.value,
            "duration_ms": r.duration_ms,
            "start_time_ms": r.start_time_ms,
            "has_audio": bool(r.audio_path and Path(r.audio_path).exists()),
            "is_trash": r.is_trash,
            "has_local_transcript": r.local_transcript is not None,
            "origin": r.origin or "plaud",
            "transcript": transcript,
            "imported_transcript": imported_transcript,
            "speakers": speakers,
            "speaker_names": speaker_names,
            # Whether both raw and corrected views exist (drives the toggle).
            "has_corrected": corrected is not None,
            "corrected_revision": corrected.revision if corrected is not None else None,
            "preview_revision": preview_revision.revision if preview_revision else None,
            "revisions": [
                {
                    "revision": row.revision,
                    "note": row.note or "Transcript correction",
                    "reason": _transcript_revision_reason(row.note),
                    "kind": row.kind,
                    "provider": row.provider,
                    "model": row.model,
                    "prompt_version": row.prompt_version,
                    "created_at": row.created_at.strftime("%b %d, %Y · %H:%M"),
                    "current": corrected is not None and row.id == corrected.id,
                }
                for row in reversed(revision_rows)
            ],
            # Edits always build on the latest canonical; hide the edit UI when
            # viewing the raw artifact behind an existing correction chain.
            "can_edit": (
                transcript is not None
                and preview_revision is None
                and (corrected is None or show_corrected)
            ),
            "summaries": summaries,
            "mind_map_out_of_date": mind_map_out_of_date,
            "mind_map_rebuild_available": mind_map_rebuild_available,
            "mind_map_rebuild_in_progress": mind_map_rebuild_in_progress,
            "mind_map_rebuild_error": mind_map_rebuild_error,
            "error": r.error,
            "retry": {
                "count": r.pipeline_retry_count or 0,
                "maximum": settings.pipeline.retry_max_attempts,
                "next_at": (
                    r.pipeline_next_retry_at.strftime("%b %d, %Y · %H:%M:%S UTC")
                    if r.pipeline_next_retry_at
                    else None
                ),
                "exhausted": (
                    r.status.value in _ATTENTION_STATES
                    and (r.pipeline_retry_count or 0) >= settings.pipeline.retry_max_attempts
                ),
            },
            "folder": _organization_item(r.folder) if r.folder is not None else None,
            "tags": [
                _organization_item(tag)
                for tag in sorted(r.tags, key=lambda tag: (tag.name.casefold(), tag.id))
            ],
            "user_notes": [
                {
                    "id": note.id,
                    "version": note.version,
                    "title": note.title,
                    "content_md": note.content_md,
                    "source_type": note.source_type,
                    "source_label": _note_source_label(note.source_type),
                    "source_summary_id": note.source_summary_id,
                    "citations": note.citations or [],
                }
                for note in sorted(
                    r.user_notes,
                    key=lambda item: (item.updated_at, item.id),
                    reverse=True,
                )
            ],
            "selected_note_id": (
                note_id if any(note.id == note_id for note in r.user_notes) else None
            ),
            "note_template_key": r.note_template_key or settings.pipeline.summary_template,
            "stages": [
                {
                    "name": stage.stage.value,
                    "status": stage.status.value,
                    "attempts": stage.attempts,
                    "provider": stage.provider,
                    "model": stage.model,
                    "source": stage.artifact_source,
                    "detail": stage.detail or {},
                    "error": stage.error,
                }
                for stage in r.stage_runs
            ],
            "usage": {
                "estimated_cost_usd": round(
                    sum(item.estimated_cost_usd or 0 for item in attempt_rows), 6
                ),
                "latency_ms": sum(item.latency_ms or 0 for item in attempt_rows),
                "audio_seconds": round(
                    sum(
                        float((item.usage or {}).get("audio_seconds") or 0) for item in attempt_rows
                    ),
                    2,
                ),
                "attempts": [
                    {
                        "stage": item.stage.value,
                        "attempt": item.attempt,
                        "status": item.status.value,
                        "provider": item.provider,
                        "model": item.model,
                        "latency_ms": item.latency_ms,
                        "usage": item.usage or {},
                        "fallback": (item.resolved_profile_snapshot or {}).get("fallback"),
                        "estimated_cost_usd": item.estimated_cost_usd or 0,
                    }
                    for item in attempt_rows
                ],
            },
            "local_data": {
                "audio_bytes": (
                    Path(r.audio_path).stat().st_size
                    if r.audio_path and Path(r.audio_path).exists()
                    else 0
                ),
                "transcripts": sum(item.source == "local" for item in r.transcripts),
                "revisions": sum(item.source == "local" for item in r.transcript_revisions),
                "notes": sum(item.source == "local" for item in r.summaries),
                "chunks": len(r.chunks),
                "stages": len(r.stage_runs),
            },
        }
        profile_rows = list(
            session.scalars(
                select(ExecutionProfile).order_by(
                    ExecutionProfile.is_system_default.desc(),
                    ExecutionProfile.name,
                    ExecutionProfile.version.desc(),
                )
            )
        )
        override = session.get(RecordingProfileOverride, file_id)
        f["profiles"] = [
            {
                "id": profile.id,
                "label": f"{profile.name} · v{profile.version}",
                "default": profile.is_system_default,
            }
            for profile in profile_rows
        ]
        f["profile_id"] = override.profile_id if override is not None else None
        from ..providers.service import preview_resolution, resolve_recording_profile
        from ..providers.usage import cost_budget_status

        try:
            profile_resolution = resolve_recording_profile(session, file_id).to_dict()
            f["profile_resolution_error"] = None
        except ValueError as exc:
            f["profile_resolution_error"] = sanitize_error(exc)
            profile_resolution = preview_resolution(session).to_dict()
        f["profile_resolution"] = profile_resolution
        f["budget"] = cost_budget_status(
            session, file_id, profile_resolution
        )
        f["note_templates"] = [
            {
                "key": item.key,
                "name": item.name,
                "version": item.version,
            }
            for item in session.scalars(
                select(NoteTemplate)
                .where(NoteTemplate.is_active.is_(True))
                .order_by(NoteTemplate.name)
            )
        ]
        recent_ask_threads = list(
            session.scalars(
                select(AskThread)
                .where(AskThread.file_id == file_id)
                .order_by(AskThread.updated_at.desc())
                .limit(5)
            )
        )
        selected_ask_thread = session.get(AskThread, ask_thread) if ask_thread else None
        if selected_ask_thread is not None and selected_ask_thread.file_id != file_id:
            selected_ask_thread = None
        f["ask_threads"] = [{"id": row.id, "title": row.title} for row in recent_ask_threads]
        f["ask_skills"] = list_ask_skills()
        f["selected_ask_thread"] = (
            thread_to_dict(selected_ask_thread) if selected_ask_thread is not None else None
        )
        organization = _organization_summary(session)
        timezone_name = get_workspace_preferences(session)["timezone"]
        filelist_query = _library_query(filelist_params, timezone_name)
        filelist_total = (
            session.scalar(
                select(func.count()).select_from(filelist_query.order_by(None).subquery())
            )
            or 0
        )
        filelist_page_size = 100
        filelist_pages = max(1, (filelist_total + filelist_page_size - 1) // filelist_page_size)
        filelist_page = min(filelist_page, filelist_pages)
        file_rows = list(
            session.scalars(
                filelist_query.offset((filelist_page - 1) * filelist_page_size).limit(
                    filelist_page_size
                )
            )
        )
        active_pinned = all(item.id != file_id for item in file_rows)
        if active_pinned:
            file_rows.insert(0, r)
        files = _file_summaries(session, file_rows)
        if active_pinned and files:
            files[0]["pinned"] = True
        previous_return_to = (
            _library_return_url_for_page(return_to, filelist_page - 1)
            if filelist_page > 1
            else None
        )
        next_return_to = (
            _library_return_url_for_page(return_to, filelist_page + 1)
            if filelist_page < filelist_pages
            else None
        )
        filelist_context = {
            "title": _library_context_title(filelist_params, organization),
            "total": filelist_total,
            "params": filelist_params,
            "page": filelist_page,
            "pages": filelist_pages,
            "previous_url": (
                _file_workspace_url(
                    file_id,
                    previous_return_to,
                    tab=active_tab,
                    view=view,
                    ask_thread=ask_thread,
                    revision=revision,
                    note_id=note_id,
                )
                if previous_return_to
                else None
            ),
            "next_url": (
                _file_workspace_url(
                    file_id,
                    next_return_to,
                    tab=active_tab,
                    view=view,
                    ask_thread=ask_thread,
                    revision=revision,
                    note_id=note_id,
                )
                if next_return_to
                else None
            ),
        }
    ctx = _base_ctx(request, "recordings") | {
        "f": f,
        "files": files,
        "q": filelist_params["q"],
        "filelist_context": filelist_context,
        "active_tab": active_tab,
        "organization": organization,
        "preserve_filelist": preserve_filelist or not workspace,
        "return_to": return_to,
        "return_to_param": return_to_param,
        "playback_second": playback_second,
        "segment_edit_error": segment_edit_error,
    }
    return templates.TemplateResponse(request=request, name="detail.html", context=ctx)


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    settings = get_settings()
    with session_scope() as session:
        counts = dict(
            session.execute(select(PlaudFile.status, func.count()).group_by(PlaudFile.status)).all()
        )
        stats = _stats(session)
        stage_counts = list(
            session.execute(
                select(StageRun.stage, StageRun.status, func.count())
                .group_by(StageRun.stage, StageRun.status)
                .order_by(StageRun.stage, StageRun.status)
            ).all()
        )
        recent_stage_issues = list(
            session.execute(
                select(
                    StageRun,
                    func.coalesce(PlaudFile.local_title, PlaudFile.filename),
                )
                .join(PlaudFile, PlaudFile.id == StageRun.file_id)
                .where(StageRun.status.in_([StageStatus.degraded, StageStatus.failed]))
                .order_by(StageRun.updated_at.desc())
                .limit(20)
            ).all()
        )
        usage_totals = session.execute(
            select(
                func.coalesce(func.sum(StageAttempt.estimated_cost_usd), 0),
                func.coalesce(func.sum(StageAttempt.latency_ms), 0),
                func.count(StageAttempt.id),
            )
        ).one()
        auto_process_new_recordings = get_workspace_preferences(session)[
            "auto_process_new_recordings"
        ]
    status_rows = [(st.value, counts.get(st, 0)) for st in FileStatus]
    checks = _health_checks(settings)
    cfg = {
        "asr": settings.asr.provider,
        "llm": settings.llm.provider,
        "embeddings": settings.embeddings.provider,
        "diarize": settings.diarize.provider,
        "summary_template": settings.pipeline.summary_template,
        "files_per_cycle": settings.pipeline.files_per_cycle,
        "retry_policy": (
            f"{settings.pipeline.retry_max_attempts} cycles · "
            f"{settings.pipeline.retry_base_seconds}s–{settings.pipeline.retry_max_seconds}s"
        ),
        "poll_interval": settings.poller.interval_seconds,
        "auto_process": "enabled" if auto_process_new_recordings else "paused",
    }
    ctx = _base_ctx(request, "status") | {
        "status_rows": status_rows,
        "stats": stats,
        "checks": checks,
        "cfg": cfg,
        "stage_rows": [(stage.value, state.value, count) for stage, state, count in stage_counts],
        "stage_issues": [
            {
                "file_id": run.file_id,
                "filename": filename or run.file_id[:12],
                "stage": run.stage.value,
                "status": run.status.value,
                "error": run.error,
            }
            for run, filename in recent_stage_issues
        ],
        "usage_totals": {
            "estimated_cost_usd": round(float(usage_totals[0]), 4),
            "latency_hours": round(float(usage_totals[1]) / 3_600_000, 2),
            "attempts": usage_totals[2],
        },
    }
    return templates.TemplateResponse(request=request, name="status.html", context=ctx)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    from ..backup_sync import list_deliveries, list_destinations
    from ..backups import list_workspace_backups
    from ..email_integrations import list_email_integrations
    from ..integrations import list_webhook_integrations
    from ..providers.contracts import ProviderStage
    from ..providers.hardware import hardware_recommendations
    from ..providers.service import list_connections, list_models, list_profiles
    from ..remote.registry import list_workers
    from ..system_info import about_info

    settings = get_settings()
    if settings.plaud.provider == "mcp":
        from ..plaud.mcp import PlaudMcpClient

        plaud_auth = PlaudMcpClient.auth_status(settings.plaud.mcp)
    else:
        from ..plaud.oauth import OfficialTokenStore

        plaud_auth = OfficialTokenStore(
            settings.plaud.official.tokens_path,
            settings.plaud.official.refresh_url,
            settings.plaud.official.request_timeout_seconds,
        ).status()
        plaud_auth["tokens_path"] = str(settings.plaud.official.tokens_path.expanduser())
    try:
        workspace_backups = list_workspace_backups()
        backup_error = None
    except ValueError as exc:
        workspace_backups = []
        backup_error = str(exc)

    with session_scope() as session:
        backup_destinations = list_destinations(session)
        now = datetime.now(UTC)
        browser_sessions = list(
            session.scalars(
                select(BrowserSession)
                .where(BrowserSession.expires_at > now)
                .order_by(BrowserSession.last_seen_at.desc())
            )
        )
        ctx = _base_ctx(request, "settings") | {
            "connections": list_connections(session),
            "models": list_models(session),
            "profiles": list_profiles(session),
            "organization": _organization_summary(session),
            "workers": list_workers(session),
            "webhook_integrations": list_webhook_integrations(session),
            "email_integrations": list_email_integrations(session),
            "provider_stages": [stage.value for stage in ProviderStage],
            "note_templates": [
                {
                    "key": item.key,
                    "name": item.name,
                    "version": item.version,
                    "system_prompt": item.system_prompt,
                    "instructions": item.instructions,
                    "is_builtin": item.is_builtin,
                    "execution_profile_id": item.execution_profile_id,
                }
                for item in session.scalars(
                    select(NoteTemplate)
                    .where(NoteTemplate.is_active.is_(True))
                    .order_by(NoteTemplate.name)
                )
            ],
            "vocabulary_terms": [
                {
                    "id": item.id,
                    "source_text": item.source_text,
                    "replacement_text": item.replacement_text,
                    "language": item.language,
                    "case_sensitive": item.case_sensitive,
                    "enabled": item.enabled,
                }
                for item in session.scalars(
                    select(VocabularyTerm).order_by(
                        VocabularyTerm.enabled.desc(), func.lower(VocabularyTerm.source_text)
                    )
                )
            ],
            "hardware_recommendations": hardware_recommendations(),
            "plaud_auth": plaud_auth,
            "plaud_provider": settings.plaud.provider,
            "workspace_backups": workspace_backups,
            "backup_error": backup_error,
            "backup_destinations": backup_destinations,
            "enabled_backup_destinations": [
                item for item in backup_destinations if item["enabled"]
            ],
            "backup_sync_deliveries": list_deliveries(session, 30),
            "about": about_info(settings),
            "browser_sessions": [
                {
                    "id": item.id,
                    "user_agent": item.user_agent or "Unknown browser",
                    "created_at": _aware(item.created_at).isoformat(),
                    "last_seen_at": _aware(item.last_seen_at).isoformat(),
                    "expires_at": _aware(item.expires_at).isoformat(),
                    "current": getattr(request.state, "browser_session_id", None) == item.id,
                }
                for item in browser_sessions
            ],
        }
    return templates.TemplateResponse(request=request, name="settings.html", context=ctx)


@app.get("/api/plaud/auth/status")
def plaud_auth_status():
    """Non-secret status for setup/health UI."""
    settings = get_settings()
    if settings.plaud.provider == "mcp":
        from ..plaud.mcp import PlaudMcpClient

        status = PlaudMcpClient.auth_status(settings.plaud.mcp)
        login_method = "plaud-mcp-oauth"
    else:
        from ..plaud.oauth import OfficialTokenStore

        status = OfficialTokenStore(
            settings.plaud.official.tokens_path,
            settings.plaud.official.refresh_url,
            settings.plaud.official.request_timeout_seconds,
        ).status()
        login_method = "native-pkce-loopback"
    return status | {
        "provider": settings.plaud.provider,
        "login_method": login_method,
    }


@app.get("/notes", response_class=HTMLResponse)
def notes_page(request: Request):
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(UserNote).order_by(UserNote.updated_at.desc(), UserNote.id.desc())
            )
        )
        file_names = {}
        for row in rows:
            if row.file_id and row.file_id not in file_names:
                recording = session.get(PlaudFile, row.file_id)
                if recording is not None:
                    file_names[row.file_id] = recording.display_title
        notes = [
            {
                "id": row.id,
                "version": row.version,
                "file_id": row.file_id,
                "filename": file_names.get(row.file_id),
                "title": row.title,
                "content_md": row.content_md,
                "source_type": row.source_type,
                "source_label": _note_source_label(row.source_type),
                "citations": row.citations or [],
            }
            for row in rows
        ]
        recording_count = int(
            session.scalar(
                select(func.count(PlaudFile.id)).where(PlaudFile.is_trash.is_(False))
            )
            or 0
        )
    return templates.TemplateResponse(
        request=request,
        name="notes.html",
        context=_base_ctx(request, "notes")
        | {"notes": notes, "recording_count": recording_count},
    )


def _health_checks(settings) -> list[dict]:
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = ""):
        checks.append({"name": name, "ok": ok, "detail": detail})

    from ..worker.convert import ffmpeg_available
    from ..worker.diarize import health as diarization_health

    add("ffmpeg", ffmpeg_available(), "on PATH" if ffmpeg_available() else "missing")
    diarize_ok, diarize_detail = diarization_health(settings.diarize)
    add(f"diarization · {settings.diarize.provider}", diarize_ok, diarize_detail)
    for label, builder in (
        (
            f"asr · {settings.asr.provider}",
            lambda: __import__(
                "localplaud.asr.registry", fromlist=["build_provider"]
            ).build_provider(settings.asr.provider, settings.asr),
        ),
        (
            f"llm · {settings.llm.provider}",
            lambda: __import__("localplaud.llm.base", fromlist=["build_llm"]).build_llm(
                settings.llm
            ),
        ),
        (
            f"embeddings · {settings.embeddings.provider}",
            lambda: __import__(
                "localplaud.embeddings.base", fromlist=["build_embedder"]
            ).build_embedder(settings.embeddings),
        ),
    ):
        try:
            provider = builder()
            health = getattr(provider, "health", None)
            if callable(health):
                ok, detail = health()
                add(label, ok, detail)
            else:
                add(label, provider.available())
        except Exception as exc:  # noqa: BLE001
            add(label, False, str(exc)[:50])
    if settings.plaud.provider == "mcp":
        from ..plaud.mcp import PlaudMcpClient

        auth = PlaudMcpClient.auth_status(settings.plaud.mcp)
        add("plaud auth · mcp", auth["ok"], auth["detail"])
    elif settings.plaud.provider == "official":
        from ..plaud.oauth import OfficialTokenStore

        auth = OfficialTokenStore(
            settings.plaud.official.tokens_path,
            settings.plaud.official.refresh_url,
            settings.plaud.official.request_timeout_seconds,
        ).status()
        add("plaud auth · official", auth["ok"], auth["detail"])
    return checks


def _library_ask_scope(
    folder_id: str | None,
    tag_id: str | None,
    origin: str | None,
    speaker_name: str | None,
    date_from: str | None,
    date_to: str | None,
    date_timezone: str,
    file_ids: list[str] | None = None,
) -> dict | None:
    values = {
        "folder_id": folder_id,
        "tag_id": tag_id,
        "origin": origin,
        "speaker_name": speaker_name,
        "file_ids": file_ids or [],
    }
    values.update(resolve_date_scope(date_from, date_to, date_timezone))
    has_scope = any(
        values[key] not in (None, "")
        for key in (
            "folder_id",
            "tag_id",
            "origin",
            "speaker_name",
        )
    ) or bool(file_ids) or bool(values.get("scope_version"))
    return values if has_scope else None


def _ask_fragment_context(
    request: Request, thread: dict, file_id: str | None, target: str
) -> dict:
    context = _base_ctx(request, "recordings")
    with session_scope() as session:
        context["organization"] = _organization_summary(session)
        context["named_speakers"] = _named_speaker_summary(session)
    return context | {"thread": thread, "file_id": file_id, "target": target}


@app.post("/ask", response_class=HTMLResponse)
def ask(
    request: Request,
    q: str = Form(...),
    thread_id: str | None = Form(None),
    ask_folder_id: str | None = Form(None),
    ask_tag_id: str | None = Form(None),
    ask_origin: str | None = Form(None),
    ask_speaker_name: str | None = Form(None),
    ask_date_from: str | None = Form(None),
    ask_date_to: str | None = Form(None),
    ask_file_ids: Annotated[list[str] | None, Form()] = None,
):
    from ..ask_threads import ask_in_thread, get_thread

    retrieval_scope = None
    try:
        retrieval_scope = _library_ask_scope(
            ask_folder_id,
            ask_tag_id,
            ask_origin,
            ask_speaker_name,
            ask_date_from,
            ask_date_to,
            _workspace_timezone_name(),
            ask_file_ids,
        )
        thread = ask_in_thread(
            q,
            thread_id=thread_id,
            retrieval_scope=retrieval_scope,
        )
    except LookupError as exc:
        return HTMLResponse(str(exc), status_code=404)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=409)
    except Exception:  # noqa: BLE001 - provider may be unavailable
        from ..worker.qa import normalize_library_scope

        fallback_scope = normalize_library_scope(retrieval_scope)
        if thread_id:
            try:
                fallback_scope = get_thread(thread_id, file_id=None)["retrieval_scope"]
            except LookupError:
                pass
        thread = _unavailable_ask_thread(
            q,
            thread_id,
            retrieval_scope=fallback_scope,
        )
    return templates.TemplateResponse(
        request=request,
        name="_ask_thread.html",
        context=_ask_fragment_context(request, thread, None, "answer"),
    )


@app.post("/file/{file_id}/ask", response_class=HTMLResponse)
def file_ask(
    request: Request,
    file_id: str,
    q: str = Form(...),
    thread_id: str | None = Form(None),
):
    """Single-recording Ask: answer grounded only in this recording, with each
    citation rendered as a playable timestamp (handled by [data-seek] JS)."""
    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return HTMLResponse("Not found", status_code=404)

    from ..ask_threads import ask_in_thread

    try:
        thread = ask_in_thread(q, file_id=file_id, thread_id=thread_id)
    except LookupError as exc:
        return HTMLResponse(str(exc), status_code=404)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=409)
    except Exception:  # noqa: BLE001 - embeddings/LLM provider may be unavailable
        thread = _unavailable_ask_thread(q, thread_id, file_id=file_id)
    return templates.TemplateResponse(
        request=request,
        name="_ask_thread.html",
        context=_ask_fragment_context(request, thread, file_id, "file-answer"),
    )


@app.get("/api/ask/skills")
def ask_skills_catalog(scope: Literal["recording", "library"] = "recording"):
    """Reusable local quick actions. Prompts are inspectable and versioned."""
    return {"skills": list_ask_skills(scope)}


@app.post("/ask/skill", response_class=HTMLResponse)
def library_ask_skill(
    request: Request,
    skill_key: str = Form(...),
    ask_folder_id: str | None = Form(None),
    ask_tag_id: str | None = Form(None),
    ask_origin: str | None = Form(None),
    ask_speaker_name: str | None = Form(None),
    ask_date_from: str | None = Form(None),
    ask_date_to: str | None = Form(None),
    ask_file_ids: Annotated[list[str] | None, Form()] = None,
):
    """Run a read-only skill through whole-library grounded Ask."""
    try:
        skill = get_ask_skill(skill_key, "library")
    except LookupError as exc:
        return HTMLResponse(str(exc), status_code=404)
    from ..ask_threads import ask_in_thread

    retrieval_scope = None
    try:
        retrieval_scope = _library_ask_scope(
            ask_folder_id,
            ask_tag_id,
            ask_origin,
            ask_speaker_name,
            ask_date_from,
            ask_date_to,
            _workspace_timezone_name(),
            ask_file_ids,
        )
        thread = ask_in_thread(
            skill["retrieval_query"],
            display_query=skill["name"],
            instruction=skill["instruction"],
            skill_snapshot=skill,
            retrieval_scope=retrieval_scope,
        )
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=409)
    except Exception:  # noqa: BLE001 - embeddings/LLM provider may be unavailable
        from ..worker.qa import normalize_library_scope

        thread = _unavailable_ask_thread(
            skill["name"],
            None,
            retrieval_scope=normalize_library_scope(retrieval_scope),
        )
    return templates.TemplateResponse(
        request=request,
        name="_ask_thread.html",
        context=_ask_fragment_context(request, thread, None, "answer"),
    )


@app.post("/file/{file_id}/ask/skill", response_class=HTMLResponse)
def file_ask_skill(request: Request, file_id: str, skill_key: str = Form(...)):
    """Run a read-only skill through the same grounded, durable file Ask path."""
    with session_scope() as session:
        if session.get(PlaudFile, file_id) is None:
            return HTMLResponse("Not found", status_code=404)
    try:
        skill = get_ask_skill(skill_key)
    except LookupError as exc:
        return HTMLResponse(str(exc), status_code=404)
    from ..ask_threads import ask_in_thread

    try:
        thread = ask_in_thread(
            skill["retrieval_query"],
            file_id=file_id,
            display_query=skill["name"],
            instruction=skill["instruction"],
            skill_snapshot=skill,
        )
    except Exception:  # noqa: BLE001 - embeddings/LLM provider may be unavailable
        thread = _unavailable_ask_thread(skill["name"], None, file_id=file_id)
    return templates.TemplateResponse(
        request=request,
        name="_ask_thread.html",
        context=_ask_fragment_context(request, thread, file_id, "file-answer"),
    )


def _unavailable_ask_thread(
    query: str,
    thread_id: str | None,
    *,
    file_id: str | None = None,
    retrieval_scope: dict | None = None,
) -> dict:
    return {
        "thread_id": thread_id,
        "file_id": file_id,
        "title": query,
        "retrieval_scope": retrieval_scope or {},
        "messages": [
            {"id": None, "role": "user", "content": query, "sources": []},
            {
                "id": None,
                "role": "assistant",
                "content": "Ask is unavailable right now — the embeddings or language "
                "model provider could not be reached. Check Settings and try again.",
                "sources": [],
            },
        ],
    }


@app.get("/file/{file_id}/export.md")
def export_markdown(file_id: str):
    """Download a recording's transcript + summaries as Markdown."""
    from fastapi.responses import PlainTextResponse

    from ..exporter import render_markdown

    try:
        md = render_markdown(file_id)
    except ValueError:
        return JSONResponse({"error": "not found"}, status_code=404)
    return PlainTextResponse(
        md,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{file_id}.md"'},
    )


@app.get("/file/{file_id}/export/transcript.{fmt}")
def export_transcript_format(
    file_id: str,
    fmt: str,
    timestamps: bool = True,
    speakers: bool = True,
):
    """Export the canonical transcript with explicit label options."""
    from ..export_formats import render_transcript

    if fmt not in {"txt", "srt", "vtt", "docx", "pdf"}:
        raise HTTPException(status_code=404, detail="unsupported transcript format")
    try:
        content, media_type = render_transcript(
            file_id, fmt, timestamps=timestamps, speakers=speakers
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{file_id}-transcript.{fmt}"'},
    )


@app.get("/file/{file_id}/export/notes.{fmt}")
def export_notes_format(file_id: str, fmt: str):
    """Export generated and user-authored notes separately from transcript."""
    from ..export_formats import render_notes

    if fmt not in {"md", "txt", "docx", "pdf"}:
        raise HTTPException(status_code=404, detail="unsupported notes format")
    try:
        content, media_type = render_notes(file_id, fmt)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(
        content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{file_id}-notes.{fmt}"'},
    )


@app.get("/file/{file_id}/export/mind-map.png")
def export_mind_map_png(file_id: str):
    """Export the latest locally generated mind map as a complete PNG tree."""
    from ..mindmap_export import render_mind_map_png

    with session_scope() as session:
        recording = session.get(PlaudFile, file_id)
        if recording is None:
            raise HTTPException(status_code=404, detail="recording not found")
        mind_map = session.scalar(
            select(Summary)
            .where(
                Summary.file_id == file_id,
                Summary.template == "mind_map",
                Summary.source == "local",
            )
            .order_by(Summary.id.desc())
        )
        mind_map_stale = any(
            run.stage == StageName.mind_map and (run.detail or {}).get("stale")
            for run in recording.stage_runs
        )
        title = recording.display_title
    if mind_map is None:
        raise HTTPException(status_code=409, detail="local mind map is not ready")
    if mind_map_stale:
        # The saved artifact no longer reflects its inputs (notes, transcript,
        # or vocabulary changed); exporting it as current would be untruthful.
        raise HTTPException(
            status_code=409,
            detail="mind map is out of date; rebuild it from the Mind map tab first",
        )
    try:
        content = render_mind_map_png(mind_map.content_md, title=title)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(
        content,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{file_id}-mind-map.png"'},
    )


@app.get("/file/{file_id}/export/audio")
def export_original_audio(file_id: str):
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        path = Path(row.audio_path) if row and row.audio_path else None
    if path is None or not path.exists():
        raise HTTPException(status_code=409, detail="recording audio has not been imported")
    return FileResponse(
        path,
        media_type=_AUDIO_MIME.get(path.suffix.lstrip(".").lower(), "application/octet-stream"),
        filename=f"{file_id}{path.suffix}",
    )


@app.post("/file/{file_id}/reprocess", response_class=HTMLResponse)
def reprocess(file_id: str, force: bool = False):
    """Kick off a pipeline re-run for one recording in the background."""
    import threading

    from ..db.models import FileStatus
    from ..worker.pipeline import process_file, processing_claim_active, reset_pipeline_retry

    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None or not r.audio_path:
            return HTMLResponse(
                '<span style="color:var(--err)">no audio to reprocess</span>', status_code=400
            )
        if processing_claim_active(r):
            return HTMLResponse(
                '<span style="color:var(--warn)">already processing</span>', status_code=409
            )
        r.status = FileStatus.downloaded
        reset_pipeline_retry(r)

    threading.Thread(
        target=process_file, args=(file_id,), kwargs={"force": force}, daemon=True
    ).start()
    return HTMLResponse('<span style="color:var(--warn)">re-running… refresh in a moment</span>')


@app.post("/file/{file_id}/generate-notes", response_class=HTMLResponse)
def generate_recording_notes(file_id: str):
    """Regenerate notes, mind map, and index without rerunning completed speech stages."""
    import threading

    from ..worker.pipeline import (
        process_derived_artifacts,
        processing_claim_active,
        reset_pipeline_retry,
    )

    with session_scope() as session:
        recording = session.get(PlaudFile, file_id)
        if recording is None:
            return HTMLResponse("recording not found", status_code=404)
        if recording.local_transcript is None:
            return HTMLResponse("a local transcript is required first", status_code=409)
        if processing_claim_active(recording):
            return HTMLResponse("already processing", status_code=409)
        _mark_derived_stale(
            session,
            file_id,
            (StageName.summarize, StageName.mind_map, StageName.index),
        )
        for run in recording.stage_runs:
            if run.stage in {StageName.summarize, StageName.mind_map, StageName.index}:
                detail = dict(run.detail or {}) | {
                    "reason": "user requested regeneration",
                    "derived_only": True,
                }
                # A full regeneration supersedes any narrower pending rebuild.
                detail.pop("mind_map_only", None)
                run.detail = detail
        recording.status = FileStatus.partial
        reset_pipeline_retry(recording)

    threading.Thread(target=process_derived_artifacts, args=(file_id,), daemon=True).start()
    return HTMLResponse("notes and mind map queued")


def _mind_map_rebuild_eligibility_error(session, recording: PlaudFile) -> str | None:
    from ..worker.pipeline import mind_map_rebuild_source

    if recording.local_transcript is None:
        return "a local transcript is required first"
    if any(
        run.stage == StageName.summarize and (run.detail or {}).get("stale")
        for run in recording.stage_runs
    ):
        return "notes are out of date; regenerate notes instead"
    if mind_map_rebuild_source(session, recording, get_settings()) is None:
        has_notes = any(
            summary.source == "local" and summary.template != "mind_map"
            for summary in recording.summaries
        )
        return (
            "cannot determine which notes to rebuild from; regenerate notes instead"
            if has_notes
            else "generated notes are required first"
        )
    mind_map_run = next(
        (run for run in recording.stage_runs if run.stage == StageName.mind_map), None
    )
    has_stale_map = (
        mind_map_run is not None
        and bool((mind_map_run.detail or {}).get("stale"))
        and any(
            summary.source == "local" and summary.template == "mind_map"
            for summary in recording.summaries
        )
    )
    return None if has_stale_map else "mind map is already current"


@app.post("/file/{file_id}/rebuild-mind-map", response_class=HTMLResponse)
def rebuild_recording_mind_map(file_id: str):
    """Rebuild only the mind map from the current canonical transcript and notes.

    Notes, transcript revisions, speech stages, embeddings/index, and Ask are
    never touched; the displaced mind map is archived to version history. The
    stale flag clears only when the rebuild succeeds — a failure leaves the
    stage failed with its error and a scheduled retry at this same scope.
    """
    import threading

    from ..worker.pipeline import (
        PipelineAlreadyRunning,
        abandon_mind_map_rebuild,
        claim_mind_map_rebuild,
        process_mind_map_only,
        processing_claim_active,
        release_processing_claim,
    )

    claim_token = None
    with session_scope() as session:
        recording = session.get(PlaudFile, file_id)
        if recording is None:
            return HTMLResponse("recording not found", status_code=404)
        if processing_claim_active(recording):
            return HTMLResponse("already processing", status_code=409)
        if eligibility_error := _mind_map_rebuild_eligibility_error(session, recording):
            return HTMLResponse(eligibility_error, status_code=409)

    try:
        # Reserve synchronously before acknowledging the POST. Concurrent or
        # repeated requests lose the same atomic lease instead of launching a
        # thread that later discovers the conflict.
        claim_token = claim_mind_map_rebuild(file_id)
    except PipelineAlreadyRunning:
        return HTMLResponse("already processing", status_code=409)

    eligibility_error = None
    try:
        with session_scope() as session:
            recording = session.get(PlaudFile, file_id)
            if recording is None:
                raise LookupError("recording disappeared while queueing")
            eligibility_error = _mind_map_rebuild_eligibility_error(session, recording)
            if eligibility_error is None:
                recording.status = FileStatus.processing
                recording.error = None
                _mark_derived_stale(session, file_id, (StageName.mind_map,))
                for run in recording.stage_runs:
                    if run.stage == StageName.mind_map:
                        detail = dict(run.detail or {}) | {
                            "reason": "user requested mind map rebuild",
                            "mind_map_only": True,
                        }
                        # An explicit Retry starts a new narrow failure budget,
                        # while file-level retry state remains untouched.
                        for key in (
                            "derived_only",
                            "mind_map_retry_count",
                            "mind_map_next_retry_at",
                            "mind_map_last_failure_at",
                        ):
                            detail.pop(key, None)
                        run.detail = detail
    except Exception:
        abandon_mind_map_rebuild(file_id, claim_token)
        raise

    if eligibility_error is not None:
        current = eligibility_error == "mind map is already current"
        release_processing_claim(
            file_id,
            claim_token,
            status=FileStatus.done if current else FileStatus.partial,
            error=None if current else eligibility_error,
        )
        return HTMLResponse(eligibility_error, status_code=409)

    try:
        worker = threading.Thread(
            target=process_mind_map_only,
            args=(file_id,),
            kwargs={"claim_token": claim_token},
            daemon=True,
        )
        worker.start()
    except Exception:
        abandon_mind_map_rebuild(file_id, claim_token)
        return HTMLResponse(
            "could not start immediately; mind map rebuild remains queued",
            status_code=503,
        )
    return HTMLResponse("mind map rebuild queued")


@app.post("/file/{file_id}/profile")
def choose_recording_profile(file_id: str, profile_id: str = Form("")):
    from ..providers.service import clear_recording_override, select_recording_override

    with session_scope() as session:
        try:
            if profile_id:
                select_recording_override(session, file_id, int(profile_id))
            else:
                clear_recording_override(session, file_id)
        except (LookupError, ValueError):
            return JSONResponse({"error": "recording or profile not found"}, status_code=404)
    return RedirectResponse(f"/file/{file_id}", status_code=303)


@app.post("/file/{file_id}/speakers")
def rename_speaker(
    file_id: str,
    key: str = Form(...),
    name: str = Form(""),
    return_to: str = Form("/"),
):
    """Set (or clear, with an empty name) the display name for one stable
    speaker key. The key itself never changes — it is the diarization label
    stored inside the transcript segments."""
    import threading

    with session_scope() as session:
        _serialize_transcript_mutation(session)
        r = session.get(PlaudFile, file_id)
        if r is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if _recording_edit_blocked(r):
            return JSONResponse(
                {"error": "recording is processing; try again when it finishes"},
                status_code=409,
            )
        settings = get_settings()
        raw_row = _canonical_raw_row(r, settings)
        corrected = _canonical_revision(r, raw_row)
        segments = (
            corrected.segments
            if corrected is not None
            else (raw_row.segments if raw_row is not None else [])
        )
        existing = session.scalar(
            select(Speaker).where(Speaker.file_id == file_id, Speaker.key == key)
        )
        if key not in speaker_keys_from_segments(segments) and existing is None:
            return JSONResponse({"error": f"unknown speaker key: {key}"}, status_code=400)
        clean = name.strip() or None
        if existing is None:
            session.add(Speaker(file_id=file_id, key=key, display_name=clean))
        else:
            existing.display_name = clean
        session.execute(delete(Chunk).where(Chunk.file_id == file_id))
        _mark_derived_stale(
            session,
            file_id,
            (StageName.summarize, StageName.mind_map, StageName.index),
        )
        expected_revision = corrected.revision if corrected is not None else 0
        expected_names = display_names(session, file_id) | ({key: clean} if clean else {})
        if not clean:
            expected_names.pop(key, None)

    from ..worker.reindex import reindex_file

    threading.Thread(
        target=reindex_file,
        args=(file_id,),
        kwargs={
            "expected_revision": expected_revision,
            "expected_speaker_names": expected_names,
        },
        daemon=True,
    ).start()
    return_to = _validated_library_return_url(return_to)
    redirect_url = _file_workspace_url(file_id, return_to, tab="transcript")
    return RedirectResponse(url=redirect_url, status_code=303)


@app.post("/file/{file_id}/transcript/segments/{idx}")
def edit_transcript_segment(
    request: Request,
    file_id: str,
    idx: int,
    text: str = Form(...),
    base_revision: int = Form(...),
    speaker: str = Form("__unchanged__"),
    return_to: str = Form("/"),
    tab: str = Form("transcript"),
    view: str = Form("corrected"),
    t: str = Form(""),
):
    """Correct one transcript segment's text and/or speaker attribution.

    Creates the next TranscriptRevision on
    top of the current canonical transcript (latest revision, else the raw
    local ASR row, which is never modified), then re-indexes in the background
    without rerunning ASR. Summaries are not auto-regenerated — regeneration
    stays an explicit action."""
    import copy
    import threading

    accept = request.headers.get("accept", "")
    wants_json = "application/json" in accept
    wants_html = "text/html" in accept and not wants_json
    return_to = _validated_library_return_url(return_to)
    tab = tab if tab in {"transcript", "notes", "mindmap", "ask"} else "transcript"
    view = view if view in {"raw", "corrected"} else "corrected"
    playback_second = _safe_playback_second(t)

    def error_response(code: str, message: str, status_code: int):
        if not wants_html:
            return JSONResponse(
                {"error": message, "code": code}, status_code=status_code
            )
        return RedirectResponse(
            _file_workspace_url(
                file_id,
                return_to,
                tab=tab,
                view=view,
                t=playback_second,
                segment_error=code,
            ),
            status_code=303,
        )

    def workspace_redirect(*, changed: bool) -> RedirectResponse:
        return RedirectResponse(
            _file_workspace_url(
                file_id,
                return_to,
                tab=tab,
                view="corrected" if changed else view,
                t=playback_second,
            ),
            status_code=303,
        )

    with session_scope() as session:
        _serialize_transcript_mutation(session)
        r = session.get(PlaudFile, file_id)
        if r is None:
            return error_response("not_found", "not found", 404)
        if _recording_edit_blocked(r):
            return error_response(
                "recording_processing",
                "recording is processing; try again when it finishes",
                409,
            )
        settings = get_settings()
        raw_row = _canonical_raw_row(r, settings)
        corrected = _canonical_revision(r, raw_row)
        if corrected is not None:
            base_segments = corrected.segments
            base_transcript_id = corrected.base_transcript_id
            revision_source = corrected.source
        elif raw_row is not None:
            base_segments = raw_row.segments
            base_transcript_id = raw_row.id
            revision_source = raw_row.source
        else:
            return error_response("no_transcript", "no transcript to edit", 400)
        current_revision = corrected.revision if corrected is not None else 0
        if base_revision != current_revision:
            return error_response(
                "stale_revision", "transcript changed; reload before saving", 409
            )
        next_revision = max((rev.revision for rev in r.transcript_revisions), default=0) + 1
        if not 0 <= idx < len(base_segments):
            return error_response(
                "segment_not_found", "segment index out of range", 400
            )
        current_segment = base_segments[idx]
        current_speaker = current_segment.get("speaker")
        valid_speakers = set(_speaker_keys_for_editing(session, r, base_segments))
        if speaker == "__unchanged__":
            selected_speaker = current_speaker
        elif speaker == "__none__":
            selected_speaker = None
        elif speaker in valid_speakers:
            selected_speaker = speaker
        else:
            return error_response("unknown_speaker", "unknown speaker key", 400)
        text_changed = text != (current_segment.get("text") or "")
        direct_speaker_changed = selected_speaker != current_speaker
        nested_speaker_changed = not text_changed and any(
            word.get("speaker") != selected_speaker
            for word in (current_segment.get("words") or [])
        )
        speaker_changed = direct_speaker_changed or nested_speaker_changed
        if not text_changed and not speaker_changed:
            if wants_json:
                return {"changed": False, "revision": current_revision}
            return workspace_redirect(changed=False)
        segments = copy.deepcopy(base_segments)
        # Word timings/text describe the raw ASR segment and become invalid once
        # its text is edited. A speaker-only correction preserves every timed word
        # and changes attribution at both segment and word level.
        words = [] if text_changed else list(segments[idx].get("words") or [])
        for word in words:
            word["speaker"] = selected_speaker
        segments[idx] = dict(segments[idx]) | {
            "text": text,
            "speaker": selected_speaker,
            "words": words,
        }
        joined = "\n".join(
            (s.get("text") or "").strip() for s in segments if (s.get("text") or "").strip()
        )
        if direct_speaker_changed:
            from_speaker = current_speaker or "unassigned"
            to_speaker = selected_speaker or "unassigned"
            revision_note = f"reassigned segment {idx} from {from_speaker} to {to_speaker}"
            if text_changed:
                revision_note = f"edited text and {revision_note}"
        elif nested_speaker_changed:
            revision_note = (
                f"normalized segment {idx} word speakers as "
                f"{selected_speaker or 'unassigned'}"
            )
        else:
            revision_note = f"edited segment {idx}"
        session.add(
            TranscriptRevision(
                file_id=file_id,
                base_transcript_id=base_transcript_id,
                revision=next_revision,
                source=revision_source,
                segments=segments,
                text=joined,
                has_speakers=bool(speaker_keys_from_segments(segments)),
                note=revision_note,
                kind="speaker_edit" if speaker_changed else "user_edit",
            )
        )
        # Invalidate the index now: stale chunks must not serve search/Ask.
        session.execute(delete(Chunk).where(Chunk.file_id == file_id))
        _mark_derived_stale(
            session,
            file_id,
            (StageName.summarize, StageName.mind_map, StageName.index),
        )
        expected_names = display_names(session, file_id)

    from ..worker.reindex import reindex_file

    threading.Thread(
        target=reindex_file,
        args=(file_id,),
        kwargs={
            "expected_revision": next_revision,
            "expected_speaker_names": expected_names,
        },
        daemon=True,
    ).start()
    if wants_json:
        return {
            "changed": True,
            "revision": next_revision,
            "speaker": selected_speaker,
        }
    return workspace_redirect(changed=True)


@app.post("/file/{file_id}/transcript/replace")
def replace_transcript_text(
    file_id: str,
    find: str = Form(...),
    replace: str = Form(""),
    base_revision: int = Form(...),
    case_sensitive: bool = Form(False),
):
    """Replace text across canonical segments in one immutable revision."""
    import copy
    import re
    import threading

    needle = find.strip()
    if not needle:
        return JSONResponse({"error": "find text is required"}, status_code=400)
    if len(needle) > 500 or len(replace) > 5000:
        return JSONResponse({"error": "find or replacement text is too long"}, status_code=400)
    with session_scope() as session:
        _serialize_transcript_mutation(session)
        row = session.get(PlaudFile, file_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if _recording_edit_blocked(row):
            return JSONResponse(
                {"error": "recording is processing; try again when it finishes"},
                status_code=409,
            )
        raw = _canonical_raw_row(row, get_settings())
        corrected = _canonical_revision(row, raw)
        if corrected is not None:
            base_segments = corrected.segments
            has_speakers = corrected.has_speakers
            base_transcript_id = corrected.base_transcript_id
            source = corrected.source
        elif raw is not None:
            base_segments = raw.segments
            has_speakers = raw.has_speakers
            base_transcript_id = raw.id
            source = raw.source
        else:
            return JSONResponse({"error": "no transcript to edit"}, status_code=400)
        current_revision = corrected.revision if corrected else 0
        if base_revision != current_revision:
            return JSONResponse(
                {"error": "transcript changed; reload before replacing"}, status_code=409
            )
        pattern = re.compile(re.escape(needle), 0 if case_sensitive else re.IGNORECASE)
        segments = copy.deepcopy(base_segments)
        replacements = 0
        for index, segment in enumerate(segments):
            updated, count = pattern.subn(lambda _match: replace, segment.get("text") or "")
            if count:
                segments[index] = dict(segment) | {"text": updated, "words": []}
                replacements += count
        if not replacements:
            return {"replacements": 0, "revision": current_revision}
        next_revision = max((rev.revision for rev in row.transcript_revisions), default=0) + 1
        joined = "\n".join(
            (segment.get("text") or "").strip()
            for segment in segments
            if (segment.get("text") or "").strip()
        )
        session.add(
            TranscriptRevision(
                file_id=file_id,
                base_transcript_id=base_transcript_id,
                revision=next_revision,
                source=source,
                segments=segments,
                text=joined,
                has_speakers=has_speakers,
                note=f'replaced "{needle}" ({replacements} occurrence(s))',
            )
        )
        session.execute(delete(Chunk).where(Chunk.file_id == file_id))
        _mark_derived_stale(
            session, file_id, (StageName.summarize, StageName.mind_map, StageName.index)
        )
        expected_names = display_names(session, file_id)
    from ..worker.reindex import reindex_file

    threading.Thread(
        target=reindex_file,
        args=(file_id,),
        kwargs={"expected_revision": next_revision, "expected_speaker_names": expected_names},
        daemon=True,
    ).start()
    return {"replacements": replacements, "revision": next_revision}


@app.post("/file/{file_id}/transcript/revisions/{revision}/restore")
def restore_transcript_revision(
    file_id: str,
    revision: int,
    base_revision: int = Form(...),
):
    """Restore history by cloning it into a new revision; never rewrite history."""
    import copy
    import threading

    with session_scope() as session:
        _serialize_transcript_mutation(session)
        row = session.get(PlaudFile, file_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        if _recording_edit_blocked(row):
            return JSONResponse(
                {"error": "recording is processing; try again when it finishes"},
                status_code=409,
            )
        raw = _canonical_raw_row(row, get_settings())
        corrected = _canonical_revision(row, raw)
        if raw is None or corrected is None:
            return JSONResponse({"error": "no revision history to restore"}, status_code=400)
        if base_revision != corrected.revision:
            return JSONResponse(
                {"error": "transcript changed; reload before restoring"}, status_code=409
            )
        target = next(
            (
                item
                for item in row.transcript_revisions
                if item.revision == revision and item.source == raw.source
            ),
            None,
        )
        if target is None:
            return JSONResponse({"error": "revision not found"}, status_code=404)
        next_revision = max(item.revision for item in row.transcript_revisions) + 1
        session.add(
            TranscriptRevision(
                file_id=file_id,
                base_transcript_id=target.base_transcript_id or raw.id,
                revision=next_revision,
                source=target.source,
                segments=copy.deepcopy(target.segments),
                text=target.text,
                has_speakers=target.has_speakers,
                note=f"restored revision {revision}",
            )
        )
        session.execute(delete(Chunk).where(Chunk.file_id == file_id))
        _mark_derived_stale(
            session, file_id, (StageName.summarize, StageName.mind_map, StageName.index)
        )
        expected_names = display_names(session, file_id)
    from ..worker.reindex import reindex_file

    threading.Thread(
        target=reindex_file,
        args=(file_id,),
        kwargs={"expected_revision": next_revision, "expected_speaker_names": expected_names},
        daemon=True,
    ).start()
    return RedirectResponse(f"/file/{file_id}?view=corrected", status_code=303)


@app.get("/api/files/{file_id}/summaries/{summary_id}/history")
def summary_history(file_id: str, summary_id: int, limit: int = 50):
    """Archived versions of one generated output, newest first.

    Bounded at the query: only ``limit`` archived bodies are ever loaded;
    the total comes from a count, not from materializing every version.
    """
    limit = max(1, min(limit, 200))
    with session_scope() as session:
        row = session.get(Summary, summary_id)
        if row is None or row.file_id != file_id:
            raise HTTPException(status_code=404, detail="summary not found")
        chain = (
            SummaryRevision.file_id == file_id,
            SummaryRevision.template == row.template,
        )
        version_count = session.scalar(
            select(func.count()).select_from(SummaryRevision).where(*chain)
        )
        rows = list(
            session.scalars(
                select(SummaryRevision)
                .where(*chain)
                .order_by(SummaryRevision.revision.desc())
                .limit(limit)
            )
        )
        return {
            "file_id": file_id,
            "summary_id": row.id,
            "template": row.template,
            "source": row.source,
            "current": {
                "title": row.title,
                "template_version": row.template_version,
                "llm_provider": row.llm_provider,
                "model": row.model,
                "created_at": _fmt_history_time(row.created_at),
                "restored_from_revision": row.restored_from_revision,
                "lineage_label": _lineage_label(row.input_transcript_revision),
            },
            "version_count": version_count or 0,
            "versions": _note_history_entries(
                rows, content_fingerprint(row), limit=limit
            ),
        }


@app.post("/file/{file_id}/summaries/{summary_id}/versions/{revision}/restore")
def restore_summary_version_route(
    file_id: str,
    summary_id: int,
    revision: int,
    tab: str = Form("notes"),
):
    """Make an archived version live again; the displaced current is archived first.

    A content swap on the existing Summary row: nothing is queued and no
    stage is rerun. The transcript and search index stay as they are (built
    from the transcript, which this does not change). A mind map sourced
    from the restored note output is marked out of date — its input just
    changed — but the artifact is preserved and not regenerated.
    """
    with session_scope() as session:
        recording = session.get(PlaudFile, file_id)
        if recording is not None and _recording_edit_blocked(recording):
            return JSONResponse(
                {"error": "recording is processing; try again when it finishes"},
                status_code=409,
            )
        row = session.get(Summary, summary_id)
        if row is None or row.file_id != file_id:
            # The live output was regenerated (new row id) or removed since render.
            return JSONResponse(
                {"error": "notes changed; reload before restoring"}, status_code=409
            )
        if row.source != "local":
            return JSONResponse(
                {"error": "only locally generated notes keep version history"},
                status_code=400,
            )
        target = session.scalar(
            select(SummaryRevision).where(
                SummaryRevision.file_id == file_id,
                SummaryRevision.template == row.template,
                SummaryRevision.revision == revision,
            )
        )
        if target is None:
            return JSONResponse({"error": "version not found"}, status_code=404)
        restore_summary_version(session, row, target)
    safe_tab = tab if tab in {"notes", "mindmap"} else "notes"
    # Land back on the note output that was just restored, not the first one.
    note_param = f"&note=sum-{summary_id}" if safe_tab == "notes" else ""
    return RedirectResponse(
        f"/file/{file_id}?tab={safe_tab}{note_param}", status_code=303
    )
