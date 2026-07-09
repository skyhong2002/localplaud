"""FastAPI app — browse recordings, read transcripts/summaries, ask questions."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..config import get_settings
from ..db.models import PlaudFile
from ..db.session import init_db, session_scope

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

app = FastAPI(title="localplaud", docs_url="/api/docs")


@app.on_event("startup")
def _startup() -> None:
    init_db()


def _fmt_dt(ms: int | None) -> str:
    if not ms:
        return ""
    from datetime import datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def _fmt_dur(ms: int | None) -> str:
    if not ms:
        return ""
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


templates.env.filters["dt"] = _fmt_dt
templates.env.filters["dur"] = _fmt_dur


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/api/files")
def api_files() -> JSONResponse:
    with session_scope() as session:
        rows = session.scalars(select(PlaudFile).order_by(PlaudFile.start_time_ms.desc()))
        data = [
            {
                "id": r.id,
                "filename": r.filename,
                "status": r.status.value,
                "duration_ms": r.duration_ms,
                "start_time_ms": r.start_time_ms,
                "has_transcript": r.transcript is not None,
                "has_summary": bool(r.summaries),
            }
            for r in rows
        ]
    return JSONResponse({"files": data})


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with session_scope() as session:
        rows = list(
            session.scalars(select(PlaudFile).order_by(PlaudFile.start_time_ms.desc()).limit(200))
        )
        files = [
            {
                "id": r.id,
                "filename": r.filename,
                "status": r.status.value,
                "duration_ms": r.duration_ms,
                "start_time_ms": r.start_time_ms,
                "has_transcript": r.transcript is not None,
                "has_summary": bool(r.summaries),
            }
            for r in rows
        ]
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"files": files, "public_url": get_settings().api.public_url},
    )


@app.get("/file/{file_id}", response_class=HTMLResponse)
def file_detail(request: Request, file_id: str):
    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return HTMLResponse("Not found", status_code=404)
        ctx = {
            "id": r.id,
            "filename": r.filename,
            "status": r.status.value,
            "duration_ms": r.duration_ms,
            "start_time_ms": r.start_time_ms,
            "transcript": (
                {
                    "provider": r.transcript.provider,
                    "language": r.transcript.language,
                    "segments": r.transcript.segments,
                }
                if r.transcript
                else None
            ),
            "summaries": [
                {"title": s.title, "content_md": s.content_md, "template": s.template}
                for s in r.summaries
            ],
        }
    return templates.TemplateResponse(request=request, name="detail.html", context={"f": ctx})


@app.post("/ask", response_class=HTMLResponse)
def ask(request: Request, q: str = Form(...)):
    from ..worker.qa import answer

    res = answer(q)
    return templates.TemplateResponse(
        request=request, name="_answer.html", context={"q": q, "res": res}
    )
