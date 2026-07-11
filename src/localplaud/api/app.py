"""FastAPI app — browse recordings, read transcripts/summaries, search, ask."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select, update

from ..ask_threads import thread_to_dict
from ..config import get_settings
from ..db.models import (
    AskThread,
    AutomationRun,
    Chunk,
    ExecutionProfile,
    FileStatus,
    Folder,
    ImportRun,
    NoteTemplate,
    PlaudFile,
    RecordingProfileOverride,
    Speaker,
    StageAttempt,
    StageName,
    StageRun,
    StageStatus,
    Tag,
    Transcript,
    TranscriptRevision,
    UserNote,
    VocabularyTerm,
    recording_tags,
)
from ..db.session import init_db, session_scope
from ..remote.server import resume_pending_jobs
from ..remote.server import router as worker_router
from ..store.speakers import display_names, speaker_keys_from_segments
from .automations import router as automations_router
from .imports import router as imports_router
from .note_templates import _item as note_template_item
from .note_templates import router as note_templates_router
from .notes import router as notes_router
from .providers import router as providers_router
from .vocabulary import router as vocabulary_router

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


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
app.include_router(automations_router)
app.include_router(worker_router)

_static = _HERE / "static"
if _static.exists():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """If api.auth_token is configured, require it on every request (except the
    health check) via an X-Auth-Token header or ?token= query param."""
    token = get_settings().api.auth_token
    if (
        token
        and request.url.path != "/healthz"
        and not request.url.path.startswith("/api/worker/v1")
    ):
        supplied = request.headers.get("x-auth-token") or request.query_params.get("token")
        if supplied != token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# --------------------------------------------------------------------------- #
# template helpers
# --------------------------------------------------------------------------- #


def _fmt_dt(ms: int | None) -> str:
    if not ms:
        return ""
    from datetime import datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%b %d, %Y · %H:%M")


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


def _base_ctx(request: Request, active: str) -> dict:
    return {"request": request, "active": active, "public_url": get_settings().api.public_url}


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


def _scene_label(scene: int | None) -> str:
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
) -> dict:
    """Normalize library query params, falling back to defaults on bad input."""
    sort_key = sort if sort in _SORT_COLUMNS else "recorded"
    direction = dir if dir in {"asc", "desc"} else "desc"
    state_val = state if state in _STATE_VALUES else None
    scene_val: int | None = None
    if scene not in (None, ""):
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
    }


def _library_query(params: dict):
    """Build a PlaudFile select from normalized library params."""
    column = _SORT_COLUMNS[params["sort"]]
    order = column.asc() if params["dir"] == "asc" else column.desc()
    # Stable tiebreaker so equal sort keys keep a deterministic order.
    stmt = select(PlaudFile).order_by(order, PlaudFile.id.asc())
    stmt = stmt.where(PlaudFile.is_trash.is_(params["view"] == "trash"))
    if params["view"] == "uncategorized":
        stmt = stmt.where(PlaudFile.folder_id.is_(None), ~PlaudFile.tags.any())
    if params["q"]:
        pattern = f"%{params['q']}%"
        stmt = stmt.where(
            or_(PlaudFile.local_title.ilike(pattern), PlaudFile.filename.ilike(pattern))
        )
    if params["state"] is not None:
        stmt = stmt.where(PlaudFile.status == params["state"])
    if params["scene"] is not None:
        stmt = stmt.where(PlaudFile.scene == params["scene"])
    if params["folder"] is not None:
        stmt = stmt.where(PlaudFile.folder_id == params["folder"])
    if params["tag"] is not None:
        stmt = stmt.where(PlaudFile.tags.any(Tag.id == params["tag"]))
    if params["origin"] is not None:
        stmt = stmt.where(PlaudFile.origin == params["origin"])
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
        {"value": sc, "label": _scene_label(sc), "count": n, "active": sc == params["scene"]}
        for sc, n in scene_rows
        if sc is not None
    ]
    origin_rows = session.execute(
        select(PlaudFile.origin, func.count())
        .where(PlaudFile.is_trash.is_(False))
        .group_by(PlaudFile.origin)
        .order_by(PlaudFile.origin)
    ).all()
    origins = [
        {
            "value": value or "plaud",
            "label": "Plaud cloud" if (value or "plaud") == "plaud" else "Local import",
            "count": count,
            "active": (value or "plaud") == params["origin"],
        }
        for value, count in origin_rows
    ]
    return {"trash_count": trash_count, "scenes": scenes, "origins": origins}


# --------------------------------------------------------------------------- #
# health + JSON API
# --------------------------------------------------------------------------- #


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
) -> JSONResponse:
    params = _parse_library_params(q, sort, dir, state, scene, view, folder, tag, origin)
    with session_scope() as session:
        rows = session.scalars(_library_query(params).limit(300))
        data = [_file_summary(r) for r in rows]
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
            _organization_item(row) | {"count": folder_counts.get(row.id, 0)} for row in folders
        ],
        "tags": [_organization_item(row) | {"count": tag_counts.get(row.id, 0)} for row in tags],
    }


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


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #


def _stats(session) -> dict:
    total = session.scalar(select(func.count()).select_from(PlaudFile)) or 0
    done = (
        session.scalar(
            select(func.count()).select_from(PlaudFile).where(PlaudFile.status == FileStatus.done)
        )
        or 0
    )
    processing = (
        session.scalar(
            select(func.count())
            .select_from(PlaudFile)
            .where(PlaudFile.status.in_([FileStatus.processing, FileStatus.downloading]))
        )
        or 0
    )
    total_ms = session.scalar(select(func.coalesce(func.sum(PlaudFile.duration_ms), 0))) or 0
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
        recent_files = [_file_summary(row) for row in recent_rows]
        attention_files = [_file_summary(row) for row in attention_rows]
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
):
    params = _parse_library_params(q, sort, dir, state, scene, view, folder, tag, origin)
    with session_scope() as session:
        rows = list(session.scalars(_library_query(params).limit(300)))
        files = [_file_summary(r) for r in rows]
        stats = _stats(session)
        facets = _library_facets(session, params)
        organization = _organization_summary(session)
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
    ctx = _base_ctx(request, "recordings") | {
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
    from datetime import datetime, timedelta

    def optional_int(value: str | None) -> int | None:
        try:
            return int(value) if value else None
        except ValueError:
            return None

    def date_ms(value: str | None, *, exclusive_end: bool = False) -> int | None:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
            if exclusive_end:
                parsed += timedelta(days=1)
            return int(parsed.timestamp() * 1000)
        except (TypeError, ValueError):
            return None

    filters = {
        "folder_id": optional_int(folder),
        "tag_id": optional_int(tag),
        "origin": origin if origin in {"plaud", "local"} else None,
        "date_from_ms": date_ms(date_from),
        "date_to_ms": date_ms(date_to, exclusive_end=True),
    }
    groups: list[dict] = []
    if q:
        from ..library_search import lexical_search
        from ..worker.qa import retrieve

        hits = lexical_search(q, **filters, limit=100)
        try:
            semantic_hits = retrieve(q, top_k=30)
        except Exception:  # noqa: BLE001 - embeddings/provider may be unavailable
            semantic_hits = []
        with session_scope() as session:
            stmt = select(PlaudFile.id).where(PlaudFile.is_trash.is_(False))
            if filters["folder_id"] is not None:
                stmt = stmt.where(PlaudFile.folder_id == filters["folder_id"])
            if filters["tag_id"] is not None:
                stmt = stmt.where(PlaudFile.tags.any(Tag.id == filters["tag_id"]))
            if filters["origin"] is not None:
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
            "date_from": date_from or "",
            "date_to": date_to or "",
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
    ctx = _base_ctx(request, "discover") | {
        "automation_rules": list_rules()["rules"],
        "automation_runs": list_runs(limit=50)["runs"],
        "organization": organization,
        "profiles": profiles,
        "note_templates": note_templates,
    }
    return templates.TemplateResponse(request=request, name="discover.html", context=ctx)


_AUDIO_MIME = {"mp3": "audio/mpeg", "opus": "audio/ogg", "wav": "audio/wav", "m4a": "audio/mp4"}


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

    from ..waveform import waveform_peaks

    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        path = row.audio_path if row else None
    if not path or not Path(path).exists():
        raise HTTPException(status_code=409, detail="recording audio has not been imported")
    try:
        peaks = waveform_peaks(path, buckets=buckets)
    except (subprocess.SubprocessError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"could not build waveform: {exc}") from exc
    return {"file_id": file_id, "buckets": len(peaks), "peaks": peaks}


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


def _mark_derived_stale(session, file_id: str, stages: tuple[StageName, ...]) -> None:
    """Preserve derived rows but make stale artifacts ineligible for reuse/UI."""
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
        run.detail = dict(run.detail or {}) | {"stale": True}


@app.get("/file/{file_id}", response_class=HTMLResponse)
def file_detail(
    request: Request,
    file_id: str,
    view: str | None = None,
    ask_thread: str | None = None,
    revision: int | None = None,
):
    settings = get_settings()
    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return HTMLResponse("Not found", status_code=404)
        # Default template first, then the rest.
        stale_stages = {run.stage for run in r.stage_runs if (run.detail or {}).get("stale")}
        summaries = sorted(
            [
                {
                    "title": s.title,
                    "content_md": s.content_md,
                    "template": s.template,
                    "template_name": (s.template_snapshot or {}).get("name")
                    or s.template.replace("-", " ").title(),
                    "template_version": s.template_version,
                    "source": s.source,
                    "input_transcript_revision": s.input_transcript_revision,
                    "input_transcript_source": s.input_transcript_source,
                    "lineage_label": (
                        "raw ASR"
                        if s.input_transcript_revision == 0
                        else (
                            f"transcript rev {s.input_transcript_revision}"
                            if s.input_transcript_revision is not None
                            else "legacy / unknown transcript"
                        )
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
            for key in speaker_keys_from_segments(canonical_segments)
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
                "provider": base.provider if base is not None else "local-edit",
                "language": base.language if base is not None else None,
                "source": "local",
                "segments": shown_revision.segments,
                "kind": "history" if preview_revision is not None else "corrected",
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
                    "title": note.title,
                    "content_md": note.content_md,
                    "source_type": note.source_type,
                    "citations": note.citations or [],
                }
                for note in r.user_notes
            ],
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
        f["profile_id"] = (
            override.profile_id
            if override is not None
            else next((profile.id for profile in profile_rows if profile.is_system_default), None)
        )
        from ..providers.service import resolve_recording_profile
        from ..providers.usage import cost_budget_status

        f["budget"] = cost_budget_status(
            session, file_id, resolve_recording_profile(session, file_id).to_dict()
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
        f["selected_ask_thread"] = (
            thread_to_dict(selected_ask_thread) if selected_ask_thread is not None else None
        )
        files = [
            _file_summary(x)
            for x in session.scalars(
                select(PlaudFile).order_by(PlaudFile.start_time_ms.desc()).limit(300)
            )
        ]
        organization = _organization_summary(session)
    ctx = _base_ctx(request, "recordings") | {
        "f": f,
        "files": files,
        "q": "",
        "organization": organization,
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
    from ..providers.contracts import ProviderStage
    from ..providers.hardware import hardware_recommendations
    from ..providers.service import list_connections, list_models, list_profiles
    from ..remote.registry import list_workers

    with session_scope() as session:
        ctx = _base_ctx(request, "settings") | {
            "connections": list_connections(session),
            "models": list_models(session),
            "profiles": list_profiles(session),
            "workers": list_workers(session),
            "provider_stages": [stage.value for stage in ProviderStage],
            "note_templates": [
                {
                    "key": item.key,
                    "name": item.name,
                    "version": item.version,
                    "system_prompt": item.system_prompt,
                    "instructions": item.instructions,
                    "is_builtin": item.is_builtin,
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
        }
    return templates.TemplateResponse(request=request, name="settings.html", context=ctx)


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
                "file_id": row.file_id,
                "filename": file_names.get(row.file_id),
                "title": row.title,
                "content_md": row.content_md,
                "source_type": row.source_type,
                "citations": row.citations or [],
            }
            for row in rows
        ]
    return templates.TemplateResponse(
        request=request,
        name="notes.html",
        context=_base_ctx(request, "notes") | {"notes": notes},
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
    has_creds = bool(settings.plaud.token or settings.plaud.cookie)
    add("plaud auth", has_creds, "configured" if has_creds else "run `auth import`")
    return checks


@app.post("/ask", response_class=HTMLResponse)
def ask(request: Request, q: str = Form(...), thread_id: str | None = Form(None)):
    from ..ask_threads import ask_in_thread

    try:
        thread = ask_in_thread(q, thread_id=thread_id)
    except LookupError as exc:
        return HTMLResponse(str(exc), status_code=404)
    except ValueError as exc:
        return HTMLResponse(str(exc), status_code=409)
    except Exception:  # noqa: BLE001 - provider may be unavailable
        thread = _unavailable_ask_thread(q, thread_id)
    return templates.TemplateResponse(
        request=request,
        name="_ask_thread.html",
        context={"thread": thread, "file_id": None, "target": "answer"},
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
        context={"thread": thread, "file_id": file_id, "target": "file-answer"},
    )


def _unavailable_ask_thread(
    query: str, thread_id: str | None, *, file_id: str | None = None
) -> dict:
    return {
        "thread_id": thread_id,
        "file_id": file_id,
        "title": query,
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

    if fmt not in {"txt", "srt", "vtt"}:
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

    if fmt not in {"md", "txt"}:
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
    from ..worker.pipeline import process_file, reset_pipeline_retry

    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None or not r.audio_path:
            return HTMLResponse(
                '<span style="color:var(--err)">no audio to reprocess</span>', status_code=400
            )
        r.status = FileStatus.downloaded
        reset_pipeline_retry(r)

    threading.Thread(
        target=process_file, args=(file_id,), kwargs={"force": force}, daemon=True
    ).start()
    return HTMLResponse('<span style="color:var(--warn)">re-running… refresh in a moment</span>')


@app.post("/file/{file_id}/profile")
def choose_recording_profile(file_id: str, profile_id: int = Form(...)):
    from ..providers.service import select_recording_override

    with session_scope() as session:
        try:
            select_recording_override(session, file_id, profile_id)
        except LookupError:
            return JSONResponse({"error": "recording or profile not found"}, status_code=404)
    return RedirectResponse(f"/file/{file_id}", status_code=303)


@app.post("/file/{file_id}/speakers")
def rename_speaker(file_id: str, key: str = Form(...), name: str = Form("")):
    """Set (or clear, with an empty name) the display name for one stable
    speaker key. The key itself never changes — it is the diarization label
    stored inside the transcript segments."""
    import threading

    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return JSONResponse({"error": "not found"}, status_code=404)
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
    return RedirectResponse(url=f"/file/{file_id}", status_code=303)


@app.post("/file/{file_id}/transcript/segments/{idx}")
def edit_transcript_segment(
    file_id: str,
    idx: int,
    text: str = Form(...),
    base_revision: int = Form(...),
):
    """Correct one transcript segment. Creates the next TranscriptRevision on
    top of the current canonical transcript (latest revision, else the raw
    local ASR row, which is never modified), then re-indexes in the background
    without rerunning ASR. Summaries are not auto-regenerated — regeneration
    stays an explicit action."""
    import copy
    import threading

    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        settings = get_settings()
        raw_row = _canonical_raw_row(r, settings)
        corrected = _canonical_revision(r, raw_row)
        if corrected is not None:
            base_segments = corrected.segments
            has_speakers = corrected.has_speakers
            base_transcript_id = corrected.base_transcript_id
            revision_source = corrected.source
        elif raw_row is not None:
            base_segments = raw_row.segments
            has_speakers = raw_row.has_speakers
            base_transcript_id = raw_row.id
            revision_source = raw_row.source
        else:
            return JSONResponse({"error": "no transcript to edit"}, status_code=400)
        current_revision = corrected.revision if corrected is not None else 0
        if base_revision != current_revision:
            return JSONResponse(
                {"error": "transcript changed; reload before saving"},
                status_code=409,
            )
        next_revision = max((rev.revision for rev in r.transcript_revisions), default=0) + 1
        if not 0 <= idx < len(base_segments):
            return JSONResponse({"error": "segment index out of range"}, status_code=400)
        segments = copy.deepcopy(base_segments)
        # Word timings/text describe the raw ASR segment and become invalid once
        # its text is edited. Preserve segment timing/speaker, but clear stale words.
        segments[idx] = dict(segments[idx]) | {"text": text, "words": []}
        joined = "\n".join(
            (s.get("text") or "").strip() for s in segments if (s.get("text") or "").strip()
        )
        session.add(
            TranscriptRevision(
                file_id=file_id,
                base_transcript_id=base_transcript_id,
                revision=next_revision,
                source=revision_source,
                segments=segments,
                text=joined,
                has_speakers=has_speakers,
                note=f"edited segment {idx}",
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
    return RedirectResponse(url=f"/file/{file_id}", status_code=303)


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
        row = session.get(PlaudFile, file_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
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
        row = session.get(PlaudFile, file_id)
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
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
