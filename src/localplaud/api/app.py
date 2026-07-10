"""FastAPI app — browse recordings, read transcripts/summaries, search, ask."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ..config import get_settings
from ..db.models import FileStatus, PlaudFile, StageRun, StageStatus
from ..db.session import init_db, session_scope

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="localplaud", docs_url="/api/docs", lifespan=_lifespan)

_static = _HERE / "static"
if _static.exists():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """If api.auth_token is configured, require it on every request (except the
    health check) via an X-Auth-Token header or ?token= query param."""
    token = get_settings().api.auth_token
    if token and request.url.path != "/healthz":
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
    independent = get_settings().pipeline.artifact_mode == "independent"
    transcript = r.local_transcript if independent else r.transcript
    return {
        "id": r.id,
        "filename": r.filename or r.id[:12],
        "status": r.status.value,
        "duration_ms": r.duration_ms,
        "start_time_ms": r.start_time_ms,
        "scene": r.scene,
        "has_transcript": transcript is not None,
        "has_imported_transcript": r.plaud_transcript is not None,
        "has_summary": any(s.source == "local" for s in r.summaries),
        "has_imported_summary": any(s.source in {"cloud", "plaud"} for s in r.summaries),
        "has_audio": bool(r.audio_path),
        "speakers": transcript.has_speakers if transcript else False,
    }


def _base_ctx(request: Request, active: str) -> dict:
    return {"request": request, "active": active, "public_url": get_settings().api.public_url}


# --------------------------------------------------------------------------- #
# health + JSON API
# --------------------------------------------------------------------------- #


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/api/files")
def api_files() -> JSONResponse:
    with session_scope() as session:
        rows = session.scalars(select(PlaudFile).order_by(PlaudFile.start_time_ms.desc()))
        data = [_file_summary(r) for r in rows]
    return JSONResponse({"files": data})


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #


def _stats(session) -> dict:
    total = session.scalar(select(func.count()).select_from(PlaudFile)) or 0
    done = session.scalar(
        select(func.count()).select_from(PlaudFile).where(PlaudFile.status == FileStatus.done)
    ) or 0
    processing = session.scalar(
        select(func.count())
        .select_from(PlaudFile)
        .where(PlaudFile.status.in_([FileStatus.processing, FileStatus.downloading]))
    ) or 0
    total_ms = session.scalar(select(func.coalesce(func.sum(PlaudFile.duration_ms), 0))) or 0
    return {
        "total": total,
        "done": done,
        "processing": processing,
        "hours": round(total_ms / 3_600_000, 1),
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str | None = None):
    with session_scope() as session:
        stmt = select(PlaudFile).order_by(PlaudFile.start_time_ms.desc())
        if q:
            stmt = stmt.where(PlaudFile.filename.ilike(f"%{q}%"))
        rows = list(session.scalars(stmt.limit(300)))
        files = [_file_summary(r) for r in rows]
        stats = _stats(session)
    ctx = _base_ctx(request, "recordings") | {"files": files, "stats": stats, "q": q or ""}
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str | None = None):
    groups: list[dict] = []
    if q:
        from ..worker.qa import retrieve

        try:
            hits = retrieve(q, top_k=20)
        except Exception:  # noqa: BLE001 - embeddings/provider may be unavailable
            hits = []
        by_file: dict[str, dict] = {}
        for h in hits:
            g = by_file.setdefault(
                h["file_id"], {"file_id": h["file_id"], "filename": h["filename"], "hits": []}
            )
            g["hits"].append(h)
        groups = sorted(by_file.values(), key=lambda g: -max(x["score"] for x in g["hits"]))
    ctx = _base_ctx(request, "search") | {"q": q or "", "groups": groups}
    return templates.TemplateResponse(request=request, name="search.html", context=ctx)


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


@app.get("/file/{file_id}", response_class=HTMLResponse)
def file_detail(request: Request, file_id: str):
    independent = get_settings().pipeline.artifact_mode == "independent"
    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return HTMLResponse("Not found", status_code=404)
        # Default template first, then the rest.
        summaries = sorted(
            ({"title": s.title, "content_md": s.content_md, "template": s.template, "source": s.source}
             for s in r.summaries),
            key=lambda s: (s["template"] != "default", s["template"]),
        )
        transcript = None
        imported_transcript = None
        speakers: list[str] = []
        canonical_row = r.local_transcript if independent else r.transcript
        if canonical_row:
            for seg in canonical_row.segments:
                sp = seg.get("speaker")
                if sp and sp not in speakers:
                    speakers.append(sp)
            transcript = {
                "provider": canonical_row.provider,
                "language": canonical_row.language,
                "source": canonical_row.source,
                "segments": canonical_row.segments,
            }
        imported_row = r.plaud_transcript
        if imported_row is not None and imported_row is not canonical_row:
            imported_transcript = {
                "provider": imported_row.provider,
                "language": imported_row.language,
                "source": imported_row.source,
                "segments": imported_row.segments,
            }
        f = {
            "id": r.id,
            "filename": r.filename or r.id[:12],
            "status": r.status.value,
            "duration_ms": r.duration_ms,
            "start_time_ms": r.start_time_ms,
            "has_audio": bool(r.audio_path and Path(r.audio_path).exists()),
            "transcript": transcript,
            "imported_transcript": imported_transcript,
            "speakers": speakers,
            "summaries": summaries,
            "error": r.error,
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
        }
        files = [
            _file_summary(x)
            for x in session.scalars(
                select(PlaudFile).order_by(PlaudFile.start_time_ms.desc()).limit(300)
            )
        ]
    ctx = _base_ctx(request, "recordings") | {"f": f, "files": files, "q": ""}
    return templates.TemplateResponse(request=request, name="detail.html", context=ctx)


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    settings = get_settings()
    with session_scope() as session:
        counts = dict(
            session.execute(
                select(PlaudFile.status, func.count()).group_by(PlaudFile.status)
            ).all()
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
                select(StageRun, PlaudFile.filename)
                .join(PlaudFile, PlaudFile.id == StageRun.file_id)
                .where(StageRun.status.in_([StageStatus.degraded, StageStatus.failed]))
                .order_by(StageRun.updated_at.desc())
                .limit(20)
            ).all()
        )
    status_rows = [(st.value, counts.get(st, 0)) for st in FileStatus]
    checks = _health_checks(settings)
    cfg = {
        "asr": settings.asr.provider,
        "llm": settings.llm.provider,
        "embeddings": settings.embeddings.provider,
        "diarize": settings.diarize.provider,
        "summary_template": settings.pipeline.summary_template,
        "files_per_cycle": settings.pipeline.files_per_cycle,
        "poll_interval": settings.poller.interval_seconds,
    }
    ctx = _base_ctx(request, "status") | {
        "status_rows": status_rows,
        "stats": stats,
        "checks": checks,
        "cfg": cfg,
        "stage_rows": [
            (stage.value, state.value, count) for stage, state, count in stage_counts
        ],
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
    }
    return templates.TemplateResponse(request=request, name="status.html", context=ctx)


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
        (f"asr · {settings.asr.provider}", lambda: __import__("localplaud.asr.registry", fromlist=["build_provider"]).build_provider(settings.asr.provider, settings.asr)),
        (f"llm · {settings.llm.provider}", lambda: __import__("localplaud.llm.base", fromlist=["build_llm"]).build_llm(settings.llm)),
        (f"embeddings · {settings.embeddings.provider}", lambda: __import__("localplaud.embeddings.base", fromlist=["build_embedder"]).build_embedder(settings.embeddings)),
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
def ask(request: Request, q: str = Form(...)):
    from ..worker.qa import answer

    res = answer(q)
    return templates.TemplateResponse(
        request=request, name="_answer.html", context={"q": q, "res": res}
    )


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


@app.post("/file/{file_id}/reprocess", response_class=HTMLResponse)
def reprocess(file_id: str, force: bool = False):
    """Kick off a pipeline re-run for one recording in the background."""
    import threading

    from ..db.models import FileStatus
    from ..worker.pipeline import process_file

    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None or not r.audio_path:
            return HTMLResponse(
                '<span style="color:var(--err)">no audio to reprocess</span>', status_code=400
            )
        r.status = FileStatus.downloaded

    threading.Thread(target=process_file, args=(file_id,), kwargs={"force": force}, daemon=True).start()
    return HTMLResponse('<span style="color:var(--warn)">re-running… refresh in a moment</span>')
