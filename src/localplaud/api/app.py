"""FastAPI app — browse recordings, read transcripts/summaries, search, ask."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import UTC
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from ..config import get_settings
from ..db.models import (
    AskMessage,
    AskThread,
    Chunk,
    ExecutionProfile,
    FileStatus,
    LocalNote,
    NoteTemplate,
    PlaudFile,
    ProviderConnection,
    ProviderHealthCheck,
    ProviderModel,
    Speaker,
    StageAttempt,
    StageName,
    StageRun,
    StageStatus,
    Summary,
    Transcript,
    TranscriptRevision,
    VocabularyTerm,
)
from ..db.session import init_db, session_scope
from ..providers.hardware import detect_hardware, recommendations
from ..providers.health import check_provider
from ..providers.materialize import materialize_snapshot
from ..providers.resolver import ProfileResolutionError, resolve_profile
from ..store.speakers import display_names, speaker_keys_from_segments

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="localplaud", docs_url="/api/docs", lifespan=_lifespan)
_sync_lock = __import__("threading").Lock()
_sync_state: dict[str, object] = {"running": False, "result": None, "error": None}

_static = _HERE / "static"
if _static.exists():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """If api.auth_token is configured, require it on every request (except the
    health check) via an X-Auth-Token header or ?token= query param."""
    token = get_settings().api.auth_token
    signed_worker_input = request.url.path.startswith("/api/worker-input/")
    if token and request.url.path != "/healthz" and not signed_worker_input:
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
        "scene_label": _scene_label(r.scene),
        "is_trash": r.is_trash,
        "needs_attention": r.status.value in _ATTENTION_STATES,
        "has_transcript": transcript is not None,
        "has_imported_transcript": r.plaud_transcript is not None,
        "has_summary": any(s.source == "local" for s in r.summaries),
        "has_imported_summary": any(s.source in {"cloud", "plaud"} for s in r.summaries),
        "has_audio": bool(r.audio_path),
        "speakers": transcript.has_speakers if transcript else False,
    }


def _base_ctx(request: Request, active: str) -> dict:
    # Sidebar file counts (All files / Trash) are part of the persistent shell.
    with session_scope() as session:
        all_count = session.scalar(
            select(func.count()).select_from(PlaudFile).where(PlaudFile.is_trash.is_(False))
        ) or 0
        trash_count = session.scalar(
            select(func.count()).select_from(PlaudFile).where(PlaudFile.is_trash.is_(True))
        ) or 0
    return {
        "request": request,
        "active": active,
        "public_url": get_settings().api.public_url,
        "counts": {"all": all_count, "trash": trash_count},
    }


# --------------------------------------------------------------------------- #
# library sorting / filtering
# --------------------------------------------------------------------------- #

_SORT_COLUMNS = {
    "recorded": PlaudFile.start_time_ms,
    "name": PlaudFile.filename,
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
    view_val = view if view in {"all", "trash"} else "all"
    return {
        "q": q or "",
        "sort": sort_key,
        "dir": direction,
        "state": state_val,
        "scene": scene_val,
        "view": view_val,
    }


def _library_query(params: dict):
    """Build a PlaudFile select from normalized library params."""
    column = _SORT_COLUMNS[params["sort"]]
    order = column.asc() if params["dir"] == "asc" else column.desc()
    # Stable tiebreaker so equal sort keys keep a deterministic order.
    stmt = select(PlaudFile).order_by(order, PlaudFile.id.asc())
    stmt = stmt.where(PlaudFile.is_trash.is_(params["view"] == "trash"))
    if params["q"]:
        stmt = stmt.where(PlaudFile.filename.ilike(f"%{params['q']}%"))
    if params["state"] is not None:
        stmt = stmt.where(PlaudFile.status == params["state"])
    if params["scene"] is not None:
        stmt = stmt.where(PlaudFile.scene == params["scene"])
    return stmt


def _library_facets(session, params: dict) -> dict:
    """Cheap aggregate context: trash count and distinct capture-source scenes."""
    trash_count = session.scalar(
        select(func.count()).select_from(PlaudFile).where(PlaudFile.is_trash.is_(True))
    ) or 0
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
    return {"trash_count": trash_count, "scenes": scenes}


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
) -> JSONResponse:
    params = _parse_library_params(q, sort, dir, state, scene, view)
    with session_scope() as session:
        rows = session.scalars(_library_query(params).limit(300))
        data = [_file_summary(r) for r in rows]
    return JSONResponse({"files": data})


@app.get("/api/files/{file_id}/stage-runs")
def api_file_stage_runs(file_id: str) -> JSONResponse:
    with session_scope() as session:
        if session.get(PlaudFile, file_id) is None:
            return JSONResponse({"error": "recording not found"}, status_code=404)
        runs = list(session.scalars(select(StageRun).where(
            StageRun.file_id == file_id
        ).order_by(StageRun.id)))
        attempts = list(session.scalars(select(StageAttempt).where(
            StageAttempt.file_id == file_id
        ).order_by(StageAttempt.id)))
        data = [{"stage": row.stage.value, "status": row.status.value,
                 "attempts": row.attempts,
                 "selected": _public_snapshot(row.profile_snapshot.get("route", {})),
                 "actual_provider": row.provider, "actual_model": row.model,
                 "artifact_source": row.artifact_source, "latency_ms": row.latency_ms,
                 "usage": row.usage, "estimated_cost": row.estimated_cost,
                 "actual_cost": row.actual_cost,
                 "detail": row.detail, "error": row.error} for row in runs]
        history = [{"id": row.id, "stage": row.stage.value, "attempt": row.attempt,
                    "status": row.status.value,
                    "selected": _public_snapshot(row.profile_snapshot.get("route", {})),
                    "actual_provider": row.provider, "actual_model": row.model,
                    "artifact_source": row.artifact_source, "latency_ms": row.latency_ms,
                    "usage": row.usage, "estimated_cost": row.estimated_cost,
                    "actual_cost": row.actual_cost,
                    "detail": row.detail, "error": row.error} for row in attempts]
    return JSONResponse({"file_id": file_id, "stages": data, "attempt_history": history})


@app.get("/api/worker-input/{file_id}")
def api_worker_input(file_id: str, expires: int, sha256: str, signature: str):
    """Serve one short-lived audio object to an authenticated remote worker."""
    import hashlib
    import hmac
    import time

    from ..asr.remote_worker_provider import sign_input

    settings = get_settings()
    secrets = [value for value in (
        settings.remote_input.signing_secret,
        settings.asr.remote_worker.input_signing_secret,
    ) if value]
    if not secrets or expires < int(time.time()) or expires > int(time.time()) + 3600:
        return JSONResponse({"error": "invalid or expired worker input"}, status_code=403)
    if not any(hmac.compare_digest(signature, sign_input(file_id, expires, sha256, secret))
               for secret in secrets):
        return JSONResponse({"error": "invalid or expired worker input"}, status_code=403)
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        path = Path(row.wav_path or row.audio_path) if row and (row.wav_path or row.audio_path) else None
    if path is None or not path.is_file():
        return JSONResponse({"error": "worker input not found"}, status_code=404)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if not hmac.compare_digest(digest, sha256):
        return JSONResponse({"error": "worker input checksum mismatch"}, status_code=409)
    return FileResponse(path, media_type="audio/wav" if path.suffix == ".wav" else "application/octet-stream")


class ProfileInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    stages: dict = Field(default_factory=dict)
    policy: dict = Field(default_factory=dict)
    enabled: bool = True


class RecordingProfileInput(BaseModel):
    profile_id: int | None = None


class ConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    provider_type: str = Field(min_length=1, max_length=64)
    base_url: str | None = None
    secret_ref: str | None = None
    configuration: dict = Field(default_factory=dict)
    enabled: bool = True


class ModelInput(BaseModel):
    connection_id: int
    name: str = Field(min_length=1, max_length=256)
    capabilities: list[str] = Field(min_length=1)
    limits: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    enabled: bool = True


class VocabularyInput(BaseModel):
    term: str = Field(min_length=1, max_length=256)
    replacement: str = Field(min_length=1, max_length=256)
    language: str | None = Field(default=None, max_length=16)
    case_sensitive: bool = False
    enabled: bool = True


class TranscriptSegmentEditInput(BaseModel):
    index: int = Field(ge=0)
    text: str


class TranscriptBulkEditInput(BaseModel):
    base_revision: int = Field(ge=0)
    edits: list[TranscriptSegmentEditInput] = Field(min_length=1)


class NoteTemplateInput(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    system_prompt: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    description: str | None = None
    category: str | None = Field(default=None, max_length=64)
    language: str | None = Field(default=None, max_length=16)
    enabled: bool = True
    execution_profile_id: int | None = None


class NoteGenerationInput(BaseModel):
    template: str = Field(default="auto", min_length=1, max_length=64)


class AskThreadInput(BaseModel):
    file_id: str | None = None
    title: str | None = Field(default=None, max_length=256)


class AskMessageInput(BaseModel):
    question: str = Field(min_length=1, max_length=10_000)


class SaveAnswerInput(BaseModel):
    title: str | None = Field(default=None, max_length=512)


class ProfileContextInput(BaseModel):
    folder_profile_id: int | None = None
    rule_profile_id: int | None = None
    template_profile_id: int | None = None


def _profile_json(row: ExecutionProfile) -> dict:
    return {
        "id": row.id, "name": row.name, "description": row.description,
        "stages": row.stages, "policy": row.policy, "enabled": row.enabled,
        "system_default": row.is_system_default, "version": row.version,
    }


def _contains_inline_secret(value) -> bool:
    import re

    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(marker in normalized for marker in (
                "apikey", "token", "password", "secret", "authorization",
                "credential", "cookie", "bearer",
            )):
                return True
            if _contains_inline_secret(nested):
                return True
    if isinstance(value, list):
        return any(_contains_inline_secret(item) for item in value)
    if isinstance(value, str) and re.match(r"(?i)^\s*(bearer\s+|sk-|hf_)", value):
        return True
    return False


def _mask_secret_ref(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, reference = value.partition(":")
    tail = reference[-4:] if len(reference) >= 4 else "****"
    return f"{scheme}:****{tail}"


def _public_snapshot(value):
    if isinstance(value, dict):
        return {
            key: (_mask_secret_ref(nested) if key == "secret_ref"
                  else _public_snapshot(nested))
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_public_snapshot(item) for item in value]
    return value


def _provider_url_error(value: str | None) -> str | None:
    if not value:
        return None
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname \
            or parsed.username or parsed.password:
        return "base_url must be credential-free HTTP(S)"
    host = parsed.hostname
    if parsed.scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
        return "non-loopback provider base_url must use HTTPS"
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )}
    except OSError:
        return "provider base_url host cannot be resolved"
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global and not ip.is_loopback:
            return "provider base_url resolves to a non-global address"
    return None


_STAGE_CAPABILITY = {
    "transcribe": "asr", "align": "alignment", "diarize": "diarization",
    "correct": "correction", "summarize": "notes", "mind_map": "mind_map",
    "ask": "ask", "index": "embeddings",
}


def _profile_catalog_error(session, stages: dict, policy: dict) -> str | None:
    candidate = ExecutionProfile(name="validation", stages=stages, policy=policy, enabled=True)
    default = session.scalar(select(ExecutionProfile).where(ExecutionProfile.is_system_default))
    for stage, route in stages.items():
        try:
            resolved = resolve_profile(
                stage, system_default=default or candidate,
                recording_override=candidate if default is not None else None,
            )
        except ProfileResolutionError as exc:
            return str(exc)
        route = resolved["route"]
        for item in [route, *route.get("fallback", [])]:
            if any(key in item for key in ("base_url", "secret_ref")):
                return "base_url and secret_ref belong on a provider connection, not a profile route"
            connection_name = item.get("connection", item.get("provider"))
            connection = session.scalar(select(ProviderConnection).where(
                ProviderConnection.name == connection_name, ProviderConnection.enabled,
            ))
            if connection is None:
                return f"enabled provider connection {connection_name!r} not found"
            model_name = item.get("model")
            capability = _STAGE_CAPABILITY.get(stage)
            if model_name and capability:
                model = session.scalar(select(ProviderModel).where(
                    ProviderModel.connection_id == connection.id,
                    ProviderModel.name == model_name, ProviderModel.enabled,
                ))
                if model is None or capability not in model.capabilities:
                    return f"model {model_name!r} does not declare {capability!r} capability"
        try:
            # Connection metadata is authoritative for egress. Validate the
            # fully materialized route now so an external connection cannot be
            # saved under no-egress and fail only when a recording starts.
            materialize_snapshot(session, resolved)
        except ProfileResolutionError as exc:
            return str(exc)
    return None


@app.get("/api/provider-connections")
def api_provider_connections() -> JSONResponse:
    with session_scope() as session:
        rows = list(session.scalars(select(ProviderConnection).order_by(ProviderConnection.name)))
        data = [{
            "id": row.id, "name": row.name, "provider_type": row.provider_type,
            "base_url": row.base_url, "secret_ref": _mask_secret_ref(row.secret_ref),
            "configuration": row.configuration, "enabled": row.enabled,
            "version": row.version,
        } for row in rows]
    return JSONResponse({"connections": data})


@app.post("/api/provider-connections")
def api_create_provider_connection(payload: ConnectionInput) -> JSONResponse:
    if payload.secret_ref and not payload.secret_ref.startswith(("env:", "keychain:")):
        return JSONResponse({"error": "secret_ref must use env: or keychain:"}, status_code=400)
    if _contains_inline_secret(payload.configuration):
        return JSONResponse({"error": "inline secrets are forbidden; use secret_ref"}, status_code=400)
    if error := _provider_url_error(payload.base_url):
        return JSONResponse({"error": error}, status_code=400)
    with session_scope() as session:
        if session.scalar(select(ProviderConnection).where(ProviderConnection.name == payload.name)):
            return JSONResponse({"error": "connection name already exists"}, status_code=409)
        row = ProviderConnection(**payload.model_dump())
        session.add(row)
        session.flush()
        data = {"id": row.id, "name": row.name, "provider_type": row.provider_type,
                "base_url": row.base_url, "secret_ref": _mask_secret_ref(row.secret_ref),
                "configuration": row.configuration, "enabled": row.enabled, "version": row.version}
    return JSONResponse(data, status_code=201)


@app.put("/api/provider-connections/{connection_id}")
def api_update_provider_connection(connection_id: int, payload: ConnectionInput) -> JSONResponse:
    if payload.secret_ref and not payload.secret_ref.startswith(("env:", "keychain:")):
        return JSONResponse({"error": "secret_ref must use env: or keychain:"}, status_code=400)
    if _contains_inline_secret(payload.configuration):
        return JSONResponse({"error": "inline secrets are forbidden; use secret_ref"}, status_code=400)
    if error := _provider_url_error(payload.base_url):
        return JSONResponse({"error": error}, status_code=400)
    with session_scope() as session:
        row = session.get(ProviderConnection, connection_id)
        if row is None:
            return JSONResponse({"error": "connection not found"}, status_code=404)
        conflict = session.scalar(select(ProviderConnection).where(
            ProviderConnection.name == payload.name, ProviderConnection.id != connection_id,
        ))
        if conflict:
            return JSONResponse({"error": "connection name already exists"}, status_code=409)
        for key, value in payload.model_dump().items():
            setattr(row, key, value)
        row.version += 1
        session.flush()
        data = {"id": row.id, "name": row.name, "provider_type": row.provider_type,
                "base_url": row.base_url, "secret_ref": _mask_secret_ref(row.secret_ref),
                "configuration": row.configuration, "enabled": row.enabled,
                "version": row.version}
    return JSONResponse(_public_snapshot(data))


@app.get("/api/provider-models")
def api_provider_models() -> JSONResponse:
    with session_scope() as session:
        rows = list(session.scalars(select(ProviderModel).order_by(ProviderModel.id)))
        data = [{"id": row.id, "connection_id": row.connection_id, "name": row.name,
                 "capabilities": row.capabilities, "limits": row.limits,
                 "metadata": row.metadata_, "enabled": row.enabled,
                 "version": row.version} for row in rows]
    return JSONResponse({"models": data})


@app.post("/api/provider-models")
def api_create_provider_model(payload: ModelInput) -> JSONResponse:
    from ..providers.contracts import CapabilityKind

    allowed = {item.value for item in CapabilityKind}
    if not set(payload.capabilities) <= allowed:
        return JSONResponse({"error": "unknown capability"}, status_code=400)
    with session_scope() as session:
        if session.get(ProviderConnection, payload.connection_id) is None:
            return JSONResponse({"error": "connection not found"}, status_code=404)
        exists = session.scalar(select(ProviderModel).where(
            ProviderModel.connection_id == payload.connection_id,
            ProviderModel.name == payload.name,
        ))
        if exists:
            return JSONResponse({"error": "model already exists for connection"}, status_code=409)
        values = payload.model_dump()
        values["metadata_"] = values.pop("metadata")
        row = ProviderModel(**values)
        session.add(row)
        session.flush()
        data = {"id": row.id, "connection_id": row.connection_id, "name": row.name,
                "capabilities": row.capabilities, "limits": row.limits,
                "metadata": row.metadata_, "enabled": row.enabled, "version": row.version}
    return JSONResponse(data, status_code=201)


@app.put("/api/provider-models/{model_id}")
def api_update_provider_model(model_id: int, payload: ModelInput) -> JSONResponse:
    from ..providers.contracts import CapabilityKind

    if not set(payload.capabilities) <= {item.value for item in CapabilityKind}:
        return JSONResponse({"error": "unknown capability"}, status_code=400)
    with session_scope() as session:
        row = session.get(ProviderModel, model_id)
        if row is None:
            return JSONResponse({"error": "model not found"}, status_code=404)
        if session.get(ProviderConnection, payload.connection_id) is None:
            return JSONResponse({"error": "connection not found"}, status_code=404)
        conflict = session.scalar(select(ProviderModel).where(
            ProviderModel.connection_id == payload.connection_id,
            ProviderModel.name == payload.name, ProviderModel.id != model_id,
        ))
        if conflict:
            return JSONResponse({"error": "model already exists for connection"}, status_code=409)
        values = payload.model_dump()
        values["metadata_"] = values.pop("metadata")
        for key, value in values.items():
            setattr(row, key, value)
        row.version += 1
        session.flush()
        data = {"id": row.id, "connection_id": row.connection_id, "name": row.name,
                "capabilities": row.capabilities, "limits": row.limits,
                "metadata": row.metadata_, "enabled": row.enabled, "version": row.version}
    return JSONResponse(_public_snapshot(data))


@app.post("/api/provider-connections/{connection_id}/health")
def api_test_provider_connection(connection_id: int, capability: str | None = None) -> JSONResponse:
    with session_scope() as session:
        row = session.get(ProviderConnection, connection_id)
        if row is None:
            return JSONResponse({"error": "connection not found"}, status_code=404)
        models = list(session.scalars(select(ProviderModel).where(
            ProviderModel.connection_id == connection_id, ProviderModel.enabled,
        )))
        if capability and not any(capability in model.capabilities for model in models):
            return JSONResponse(
                {"error": f"connection does not declare capability {capability!r}"},
                status_code=422,
            )
        if row.enabled:
            state, detail = check_provider(row.provider_type, row.secret_ref, row.base_url)
        else:
            state, detail = "unavailable", {"note": "connection is disabled/revoked"}
        detail = dict(detail) | {
            "capability": capability,
            "models": [model.name for model in models
                       if capability is None or capability in model.capabilities],
            "base_url_configured": bool(row.base_url),
            "paid_probe_sent": False,
            "enabled": row.enabled,
            "revoked": not row.enabled,
        }
        check = ProviderHealthCheck(connection_id=row.id, capability=capability,
                                    state=state, detail=detail)
        session.add(check)
        session.flush()
        data = {"connection_id": row.id, "capability": capability, "state": state,
                "detail": detail, "checked_at": check.checked_at.isoformat()}
    return JSONResponse(data)


@app.get("/api/capabilities")
def api_capabilities() -> JSONResponse:
    with session_scope() as session:
        models = list(session.scalars(select(ProviderModel).order_by(ProviderModel.id)))
        checks = list(session.scalars(
            select(ProviderHealthCheck).order_by(ProviderHealthCheck.checked_at.desc())
        ))
        latest = {}
        for check in checks:
            latest.setdefault((check.connection_id, check.capability), {
                "state": check.state, "detail": check.detail,
                "checked_at": check.checked_at.isoformat(),
            })
        data = [{"connection_id": row.connection_id, "model_id": row.id,
                 "model": row.name, "capability": capability,
                 "health": latest.get((row.connection_id, capability), {"state": "unknown"})}
                for row in models for capability in row.capabilities]
    return JSONResponse({"capabilities": data})


@app.get("/api/hardware")
def api_hardware() -> JSONResponse:
    report = detect_hardware()
    return JSONResponse({"hardware": report.to_dict(), "recommendations": recommendations(report)})


@app.get("/api/execution-profiles")
def api_execution_profiles() -> JSONResponse:
    with session_scope() as session:
        rows = list(session.scalars(select(ExecutionProfile).order_by(ExecutionProfile.name)))
        data = [_profile_json(row) for row in rows]
    return JSONResponse({"profiles": data})


@app.post("/api/execution-profiles")
def api_create_execution_profile(payload: ProfileInput) -> JSONResponse:
    if _contains_inline_secret(payload.model_dump()):
        return JSONResponse({"error": "inline secrets are forbidden; use secret_ref"}, status_code=400)
    with session_scope() as session:
        if session.scalar(select(ExecutionProfile).where(ExecutionProfile.name == payload.name)):
            return JSONResponse({"error": "profile name already exists"}, status_code=409)
        if error := _profile_catalog_error(session, payload.stages, payload.policy):
            return JSONResponse({"error": error}, status_code=422)
        row = ExecutionProfile(**payload.model_dump())
        session.add(row)
        session.flush()
        data = _profile_json(row)
    return JSONResponse(data, status_code=201)


@app.put("/api/execution-profiles/{profile_id}")
def api_update_execution_profile(profile_id: int, payload: ProfileInput) -> JSONResponse:
    if _contains_inline_secret(payload.model_dump()):
        return JSONResponse({"error": "inline secrets are forbidden; use secret_ref"}, status_code=400)
    with session_scope() as session:
        row = session.get(ExecutionProfile, profile_id)
        if row is None:
            return JSONResponse({"error": "profile not found"}, status_code=404)
        conflict = session.scalar(select(ExecutionProfile).where(
            ExecutionProfile.name == payload.name, ExecutionProfile.id != profile_id
        ))
        if conflict:
            return JSONResponse({"error": "profile name already exists"}, status_code=409)
        if error := _profile_catalog_error(session, payload.stages, payload.policy):
            return JSONResponse({"error": error}, status_code=422)
        for key, value in payload.model_dump().items():
            setattr(row, key, value)
        row.version += 1
        session.flush()
        data = _profile_json(row)
    return JSONResponse(data)


@app.get("/api/execution-profiles/resolve")
def api_resolve_execution_profile(stage: StageName, file_id: str | None = None) -> JSONResponse:
    with session_scope() as session:
        default = session.scalar(select(ExecutionProfile).where(ExecutionProfile.is_system_default))
        if default is None:
            return JSONResponse({"error": "system default is not initialized"}, status_code=503)
        override = None
        folder_rule = None
        template_default = None
        if file_id:
            file = session.get(PlaudFile, file_id)
            if file is None:
                return JSONResponse({"error": "recording not found"}, status_code=404)
            if file.execution_profile_id:
                override = session.get(ExecutionProfile, file.execution_profile_id)
            context = file.profile_context or {}
            folder_id = context.get("rule_profile_id") or context.get("folder_profile_id")
            folder_rule = session.get(ExecutionProfile, folder_id) if folder_id else None
            template_id = context.get("template_profile_id")
            template_default = session.get(ExecutionProfile, template_id) if template_id else None
        try:
            data = materialize_snapshot(session, resolve_profile(
                stage, system_default=default, folder_rule=folder_rule,
                template_default=template_default, recording_override=override
            ))
        except ProfileResolutionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
    return JSONResponse(_public_snapshot(data))


@app.put("/api/files/{file_id}/execution-profile")
def api_set_recording_profile(file_id: str, payload: RecordingProfileInput) -> JSONResponse:
    with session_scope() as session:
        file = session.get(PlaudFile, file_id)
        if file is None:
            return JSONResponse({"error": "recording not found"}, status_code=404)
        if payload.profile_id is not None:
            profile = session.get(ExecutionProfile, payload.profile_id)
            if profile is None or not profile.enabled:
                return JSONResponse({"error": "enabled profile not found"}, status_code=404)
        file.execution_profile_id = payload.profile_id
        # Preserve historical/current provenance until a stage really begins a
        # new attempt. Reused artifacts must not be relabelled as if the new
        # profile produced them.
        for run in file.stage_runs:
            detail = dict(run.detail or {})
            detail["profile_change_pending"] = True
            run.detail = detail
    return JSONResponse({"file_id": file_id, "profile_id": payload.profile_id})


@app.put("/api/files/{file_id}/profile-context")
def api_set_profile_context(file_id: str, payload: ProfileContextInput) -> JSONResponse:
    with session_scope() as session:
        file = session.get(PlaudFile, file_id)
        if file is None:
            return JSONResponse({"error": "recording not found"}, status_code=404)
        values = payload.model_dump()
        for profile_id in (value for value in values.values() if value is not None):
            profile = session.get(ExecutionProfile, profile_id)
            if profile is None or not profile.enabled:
                return JSONResponse({"error": f"enabled profile {profile_id} not found"},
                                    status_code=404)
        file.profile_context = values
        for run in file.stage_runs:
            detail = dict(run.detail or {})
            detail["profile_change_pending"] = True
            run.detail = detail
    return JSONResponse({"file_id": file_id, "profile_context": values})


@app.get("/api/vocabulary")
def api_vocabulary() -> JSONResponse:
    with session_scope() as session:
        rows = list(session.scalars(select(VocabularyTerm).order_by(VocabularyTerm.term)))
        data = [{"id": row.id, "term": row.term, "replacement": row.replacement,
                 "language": row.language, "case_sensitive": row.case_sensitive,
                 "enabled": row.enabled} for row in rows]
    return JSONResponse({"terms": data})


def _invalidate_vocabulary_dependents(session) -> None:
    session.execute(delete(Chunk))
    for run in session.scalars(select(StageRun).where(StageRun.stage.in_([
        StageName.correct, StageName.summarize, StageName.mind_map, StageName.index,
    ]))):
        detail = dict(run.detail or {})
        detail["stale"] = True
        detail["stale_reason"] = "vocabulary changed"
        run.detail = detail


@app.post("/api/vocabulary")
def api_create_vocabulary(payload: VocabularyInput) -> JSONResponse:
    with session_scope() as session:
        if session.scalar(select(VocabularyTerm).where(VocabularyTerm.term == payload.term)):
            return JSONResponse({"error": "term already exists"}, status_code=409)
        row = VocabularyTerm(**payload.model_dump())
        session.add(row)
        _invalidate_vocabulary_dependents(session)
        session.flush()
        data = {"id": row.id, **payload.model_dump()}
    return JSONResponse(data, status_code=201)


@app.delete("/api/vocabulary/{term_id}")
def api_delete_vocabulary(term_id: int) -> JSONResponse:
    with session_scope() as session:
        row = session.get(VocabularyTerm, term_id)
        if row is None:
            return JSONResponse({"error": "term not found"}, status_code=404)
        session.delete(row)
        _invalidate_vocabulary_dependents(session)
    return JSONResponse({"deleted": term_id})


def _template_json(row: NoteTemplate) -> dict:
    return {"id": row.id, "name": row.name, "system_prompt": row.system_prompt,
            "instructions": row.instructions, "description": row.description,
            "category": row.category, "language": row.language,
            "provenance": row.provenance, "version": row.version, "enabled": row.enabled,
            "execution_profile_id": row.execution_profile_id}


@app.get("/api/note-templates")
def api_note_templates() -> JSONResponse:
    with session_scope() as session:
        rows = list(session.scalars(select(NoteTemplate).order_by(NoteTemplate.name)))
        data = [_template_json(row) for row in rows]
    return JSONResponse({"templates": data})


@app.post("/api/note-templates")
def api_create_note_template(payload: NoteTemplateInput) -> JSONResponse:
    with session_scope() as session:
        if session.scalar(select(NoteTemplate).where(NoteTemplate.name == payload.name)):
            return JSONResponse({"error": "template already exists"}, status_code=409)
        if payload.execution_profile_id is not None and session.get(
            ExecutionProfile, payload.execution_profile_id
        ) is None:
            return JSONResponse({"error": "execution profile not found"}, status_code=404)
        row = NoteTemplate(**payload.model_dump(), provenance="user", version=1)
        session.add(row)
        session.flush()
        data = _template_json(row)
    return JSONResponse(data, status_code=201)


@app.put("/api/note-templates/{template_id}")
def api_update_note_template(template_id: int, payload: NoteTemplateInput) -> JSONResponse:
    with session_scope() as session:
        row = session.get(NoteTemplate, template_id)
        if row is None:
            return JSONResponse({"error": "template not found"}, status_code=404)
        conflict = session.scalar(select(NoteTemplate).where(
            NoteTemplate.name == payload.name, NoteTemplate.id != template_id,
        ))
        if conflict:
            return JSONResponse({"error": "template already exists"}, status_code=409)
        if payload.execution_profile_id is not None and session.get(
            ExecutionProfile, payload.execution_profile_id
        ) is None:
            return JSONResponse({"error": "execution profile not found"}, status_code=404)
        for key, value in payload.model_dump().items():
            setattr(row, key, value)
        row.version += 1
        row.provenance = "user" if row.provenance == "builtin" else row.provenance
        session.flush()
        data = _template_json(row)
    return JSONResponse(data)


@app.post("/api/files/{file_id}/notes/generate")
def api_generate_note(file_id: str, payload: NoteGenerationInput) -> JSONResponse:
    from ..worker.pipeline import generate_note

    if payload.template != "auto":
        with session_scope() as session:
            if session.scalar(select(NoteTemplate).where(
                NoteTemplate.name == payload.template, NoteTemplate.enabled,
            )) is None:
                return JSONResponse({"error": "enabled template not found"}, status_code=404)
    try:
        result = generate_note(file_id, payload.template)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"file_id": file_id, "template": result["template"],
                         "title": result.get("title"), "content_md": result["content_md"],
                         "provider": result.get("provider"), "model": result.get("model"),
                         "coverage": result.get("coverage", {})})


@app.post("/api/ask/threads")
def api_create_ask_thread(payload: AskThreadInput) -> JSONResponse:
    with session_scope() as session:
        if payload.file_id and session.get(PlaudFile, payload.file_id) is None:
            return JSONResponse({"error": "recording not found"}, status_code=404)
        row = AskThread(file_id=payload.file_id, title=payload.title)
        session.add(row)
        session.flush()
        data = {"id": row.id, "file_id": row.file_id, "title": row.title}
    return JSONResponse(data, status_code=201)


@app.get("/api/ask/threads/{thread_id}")
def api_get_ask_thread(thread_id: int) -> JSONResponse:
    with session_scope() as session:
        thread = session.get(AskThread, thread_id)
        if thread is None:
            return JSONResponse({"error": "thread not found"}, status_code=404)
        messages = list(session.scalars(select(AskMessage).where(
            AskMessage.thread_id == thread_id
        ).order_by(AskMessage.id)))
        data = {"id": thread.id, "file_id": thread.file_id, "title": thread.title,
                "messages": [{"id": row.id, "role": row.role, "content": row.content,
                              "citations": row.citations, "provider": row.provider,
                              "model": row.model,
                              "profile_snapshot": _public_snapshot(row.profile_snapshot),
                              "usage": row.usage, "estimated_cost": row.estimated_cost,
                              "actual_cost": row.actual_cost}
                             for row in messages]}
    return JSONResponse(data)


@app.post("/api/ask/threads/{thread_id}/messages")
def api_add_ask_message(thread_id: int, payload: AskMessageInput) -> JSONResponse:
    from ..worker.qa import answer

    with session_scope() as session:
        thread = session.get(AskThread, thread_id)
        if thread is None:
            return JSONResponse({"error": "thread not found"}, status_code=404)
        prior = list(session.scalars(select(AskMessage).where(
            AskMessage.thread_id == thread_id
        ).order_by(AskMessage.id)))
        session.add(AskMessage(thread_id=thread_id, role="user", content=payload.question))
        file_id = thread.file_id
    try:
        result = answer(payload.question, file_id=file_id,
                        history=[{"role": row.role, "content": row.content} for row in prior])
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"Ask unavailable: {exc}"}, status_code=503)
    with session_scope() as session:
        message = AskMessage(
            thread_id=thread_id, role="assistant", content=result["answer"],
            citations=result.get("sources", []), provider=result.get("provider"),
            model=result.get("model"), profile_snapshot=result.get("profile_snapshot", {}),
            usage=result.get("usage", {}),
            estimated_cost=result.get("profile_snapshot", {}).get("route", {}).get(
                "estimated_cost"
            ),
        )
        session.add(message)
        session.flush()
        data = {"id": message.id, "role": message.role, "content": message.content,
                "citations": message.citations, "provider": message.provider,
                "model": message.model,
                "profile_snapshot": _public_snapshot(message.profile_snapshot),
                "usage": message.usage, "estimated_cost": message.estimated_cost,
                "actual_cost": message.actual_cost}
    return JSONResponse(_public_snapshot(data), status_code=201)


@app.post("/api/ask/messages/{message_id}/save-note")
def api_save_answer_note(message_id: int, payload: SaveAnswerInput) -> JSONResponse:
    with session_scope() as session:
        message = session.get(AskMessage, message_id)
        if message is None or message.role != "assistant":
            return JSONResponse({"error": "assistant answer not found"}, status_code=404)
        thread = session.get(AskThread, message.thread_id)
        citations_md = "\n".join(
            f"- [{item.get('filename', item.get('file_id', 'recording'))}]"
            f"(/file/{item.get('file_id')}?t={item.get('start', 0)})"
            for item in message.citations
        )
        content = message.content + (f"\n\n## Sources\n{citations_md}" if citations_md else "")
        note = LocalNote(
            file_id=thread.file_id, title=payload.title or thread.title or "Saved Ask answer",
            content_md=content, kind="ask-answer",
            provenance={"thread_id": thread.id, "message_id": message.id,
                        "citations": message.citations, "provider": message.provider,
                        "model": message.model, "profile_snapshot": message.profile_snapshot},
        )
        session.add(note)
        session.flush()
        data = {"id": note.id, "file_id": note.file_id, "title": note.title,
                "content_md": note.content_md, "kind": note.kind,
                "provenance": note.provenance}
    return JSONResponse(_public_snapshot(data), status_code=201)


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
def home(request: Request):
    """Home — recent files plus a quick library/attention overview."""
    with session_scope() as session:
        recent = [
            _file_summary(r)
            for r in session.scalars(
                select(PlaudFile)
                .where(PlaudFile.is_trash.is_(False))
                .order_by(PlaudFile.start_time_ms.desc(), PlaudFile.id.asc())
                .limit(5)
            )
        ]
        attention = [
            _file_summary(r)
            for r in session.scalars(
                select(PlaudFile)
                .where(
                    PlaudFile.is_trash.is_(False),
                    PlaudFile.status.in_([FileStatus.error, FileStatus.partial]),
                )
                .order_by(PlaudFile.start_time_ms.desc(), PlaudFile.id.asc())
                .limit(6)
            )
        ]
        stats = _stats(session)
    ctx = _base_ctx(request, "home") | {
        "recent": recent,
        "attention": attention,
        "stats": stats,
    }
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


@app.get("/file-list", response_class=HTMLResponse)
def file_list(
    request: Request,
    categoryId: str | None = None,  # noqa: N803 - matches Plaud Web's query param
    q: str | None = None,
    sort: str | None = None,
    dir: str | None = None,
    state: str | None = None,
    scene: str | None = None,
    view: str | None = None,
):
    if categoryId == "trash":
        view = "trash"
    params = _parse_library_params(q, sort, dir, state, scene, view)
    with session_scope() as session:
        rows = list(session.scalars(_library_query(params).limit(300)))
        files = [_file_summary(r) for r in rows]
        facets = _library_facets(session, params)
    in_trash = params["view"] == "trash"
    ctx = _base_ctx(request, "trash" if in_trash else "allfiles") | {
        "files": files,
        "q": params["q"],
        "lib": params,
        "facets": facets,
        "states": [s.value for s in FileStatus],
        "attention_states": _ATTENTION_STATES,
        "category": "trash" if in_trash else "allFiles",
        "page_title": "Trash" if in_trash else "All files",
    }
    return templates.TemplateResponse(request=request, name="filelist.html", context=ctx)


@app.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request):
    ctx = _base_ctx(request, "ask")
    return templates.TemplateResponse(request=request, name="ask.html", context=ctx)


@app.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request):
    with session_scope() as session:
        tpls = [
            _template_json(row)
            for row in session.scalars(select(NoteTemplate).order_by(NoteTemplate.name))
        ]
    categories = (
        ["All"] + sorted({t["category"] or "General" for t in tpls}) if tpls else []
    )
    ctx = _base_ctx(request, "templates") | {"tpls": tpls, "categories": categories}
    return templates.TemplateResponse(request=request, name="templates.html", context=ctx)


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


def _transcript_edit_base(r: PlaudFile):
    raw_row = _canonical_raw_row(r, get_settings())
    corrected = _canonical_revision(r, raw_row)
    if corrected is not None:
        return (corrected.segments, corrected.has_speakers,
                corrected.base_transcript_id, corrected.source, corrected.revision)
    if raw_row is not None:
        return (raw_row.segments, raw_row.has_speakers, raw_row.id, raw_row.source, 0)
    return None


def _create_transcript_revision(
    session,
    r: PlaudFile,
    *,
    base_revision: int,
    segments: list[dict],
    note: str,
) -> tuple[int, dict[str, str]]:
    base = _transcript_edit_base(r)
    if base is None:
        raise ValueError("no transcript to edit")
    _base_segments, has_speakers, base_transcript_id, source, current_revision = base
    if base_revision != current_revision:
        raise RuntimeError("transcript changed; reload before saving")
    next_revision = max((rev.revision for rev in r.transcript_revisions), default=0) + 1
    joined = "\n".join(
        (segment.get("text") or "").strip()
        for segment in segments if (segment.get("text") or "").strip()
    )
    session.add(TranscriptRevision(
        file_id=r.id, base_transcript_id=base_transcript_id,
        revision=next_revision, source=source, segments=segments, text=joined,
        has_speakers=has_speakers, note=note[:256], kind="user",
    ))
    session.execute(delete(Chunk).where(Chunk.file_id == r.id))
    _mark_derived_stale(
        session, r.id, (StageName.summarize, StageName.mind_map, StageName.index),
    )
    session.flush()
    return next_revision, display_names(session, r.id)


def _queue_revision_reindex(file_id: str, revision: int, names: dict[str, str]) -> None:
    import threading

    from ..worker.reindex import reindex_file

    threading.Thread(
        target=reindex_file, args=(file_id,),
        kwargs={"expected_revision": revision, "expected_speaker_names": names},
        daemon=True,
    ).start()


@app.get("/file/{file_id}", response_class=HTMLResponse)
def file_detail(request: Request, file_id: str, view: str | None = None):
    settings = get_settings()
    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return HTMLResponse("Not found", status_code=404)
        # Put meeting notes first. Imported Plaud notes remain visibly labelled,
        # but are useful as the initial view when no local meeting note exists.
        stale_stages = {
            run.stage
            for run in r.stage_runs
            if (run.detail or {}).get("stale")
        }
        summaries = sorted(
            (
                {"title": s.title, "content_md": s.content_md, "template": s.template,
                 "source": s.source, "revision": s.revision,
                 "transcript_revision": s.transcript_revision,
                 "template_version": s.template_version}
                for s in r.summaries
                if not (
                    s.source == "local"
                    and (
                        (s.template == "mind_map" and StageName.mind_map in stale_stages)
                        or (s.template != "mind_map" and StageName.summarize in stale_stages)
                    )
                )
            ),
            key=lambda s: (
                0 if s["template"] == "meeting" and s["source"] == "local" else
                1 if s["template"] == "plaud" and s["source"] == "cloud" else
                2 if s["template"] == "default" else
                3 if s["template"] != "mind_map" else 4,
                s["template"],
            ),
        )
        generation_templates = [
            {"name": row.name, "description": row.description or "Generate a new note"}
            for row in session.scalars(
                select(NoteTemplate).where(NoteTemplate.enabled).order_by(NoteTemplate.name)
            )
        ]
        transcript = None
        imported_transcript = None
        raw_row = _canonical_raw_row(r, settings)
        corrected = _canonical_revision(r, raw_row)
        # Canonical segments (latest correction wins) drive the speaker legend.
        canonical_segments = (
            corrected.segments if corrected is not None
            else (raw_row.segments if raw_row is not None else [])
        )
        speaker_names = display_names(session, r.id)
        speakers = [
            {"key": key, "name": speaker_names.get(key)}
            for key in speaker_keys_from_segments(canonical_segments)
        ]
        show_corrected = corrected is not None and view != "raw"
        if show_corrected:
            base = (
                session.get(Transcript, corrected.base_transcript_id)
                if corrected.base_transcript_id is not None
                else raw_row
            )
            transcript = {
                "provider": base.provider if base is not None else "local-edit",
                "language": base.language if base is not None else None,
                "source": "local",
                "segments": corrected.segments,
                "kind": "corrected",
                "revision": corrected.revision,
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
        revision_history = [{
            "revision": revision.revision, "kind": revision.kind,
            "note": revision.note, "created_at": revision.created_at,
            "summary_count": session.query(Summary).filter_by(
                file_id=file_id, transcript_revision=revision.revision,
            ).count(),
            "chunk_count": session.query(Chunk).filter_by(
                file_id=file_id, transcript_revision=revision.revision,
            ).count(),
        } for revision in reversed(r.transcript_revisions)]
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
            "speaker_names": speaker_names,
            # Whether both raw and corrected views exist (drives the toggle).
            "has_corrected": corrected is not None,
            "corrected_revision": corrected.revision if corrected is not None else None,
            "revision_history": revision_history,
            # Edits always build on the latest canonical; hide the edit UI when
            # viewing the raw artifact behind an existing correction chain.
            "can_edit": transcript is not None and (corrected is None or show_corrected),
            "summaries": summaries,
            "generation_templates": generation_templates,
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
    ctx = _base_ctx(request, "allfiles") | {"f": f}
    return templates.TemplateResponse(request=request, name="detail.html", context=ctx)


def _run_sync() -> None:
    from ..poller.poll import poll_once

    try:
        result = poll_once(get_settings())
        _sync_state.update(running=False, result=result, error=None)
    except Exception as exc:  # noqa: BLE001 - reported to the operator in the UI
        _sync_state.update(running=False, result=None, error=str(exc))
    finally:
        _sync_lock.release()


@app.post("/api/sync")
def api_sync() -> JSONResponse:
    """Start one Plaud discovery/download/import cycle without requiring CLI use."""
    if not _sync_lock.acquire(blocking=False):
        return JSONResponse({"running": True, "message": "Sync is already running"}, status_code=409)
    _sync_state.update(running=True, result=None, error=None)
    __import__("threading").Thread(target=_run_sync, daemon=True).start()
    return JSONResponse({"running": True, "message": "Sync started"}, status_code=202)


@app.get("/api/sync/status")
def api_sync_status() -> JSONResponse:
    return JSONResponse(dict(_sync_state))


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


@app.post("/file/{file_id}/ask", response_class=HTMLResponse)
def file_ask(request: Request, file_id: str, q: str = Form(...)):
    """Single-recording Ask: answer grounded only in this recording, with each
    citation rendered as a playable timestamp (handled by [data-seek] JS)."""
    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return HTMLResponse("Not found", status_code=404)

    from ..worker.qa import answer

    try:
        res = answer(q, file_id=file_id)
    except Exception:  # noqa: BLE001 - embeddings/LLM provider may be unavailable
        res = {
            "answer": "Ask is unavailable right now — the embeddings or language "
            "model provider could not be reached. Check Settings and try again.",
            "sources": [],
        }
    return templates.TemplateResponse(
        request=request, name="_file_answer.html", context={"q": q, "res": res}
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


@app.get("/file/{file_id}/mindmap.png")
def export_mindmap_png(file_id: str) -> Response:
    from ..export_mindmap import render_mindmap_png

    with session_scope() as session:
        file = session.get(PlaudFile, file_id)
        if file is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        run = session.scalar(select(StageRun).where(
            StageRun.file_id == file_id, StageRun.stage == StageName.mind_map,
        ))
        if run is not None and (run.detail or {}).get("stale"):
            return JSONResponse({"error": "mind map is stale; regenerate it first"},
                                status_code=409)
        mind_map = session.scalar(select(Summary).where(
            Summary.file_id == file_id, Summary.template == "mind_map",
            Summary.source == "local",
        ).order_by(Summary.id.desc()))
        if mind_map is None:
            return JSONResponse({"error": "mind map not found"}, status_code=404)
        title = mind_map.title or file.filename or "Mind map"
        content = mind_map.content_md
    png = render_mindmap_png(content, title=title)
    return Response(
        png, media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{file_id}-mind-map.png"'},
    )


@app.post("/file/{file_id}/reprocess", response_class=HTMLResponse)
def reprocess(file_id: str, force: bool = False):
    """Kick off a pipeline re-run for one recording in the background."""
    import threading

    from ..worker.pipeline import ProcessingAlreadyRunning, _claim_processing, process_file

    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None or not r.audio_path:
            return HTMLResponse(
                '<span style="color:var(--err)">no audio to reprocess</span>', status_code=400
            )
    try:
        claim_token = _claim_processing(file_id, get_settings())
    except ProcessingAlreadyRunning:
        return HTMLResponse(
            '<span style="color:var(--warn)">already processing</span>', status_code=409
        )

    threading.Thread(
        target=process_file, args=(file_id,),
        kwargs={"force": force, "claim_token": claim_token}, daemon=True,
    ).start()
    return HTMLResponse('<span style="color:var(--warn)">re-running… refresh in a moment</span>')


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
            corrected.segments if corrected is not None
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
    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        base = _transcript_edit_base(r)
        if base is None:
            return JSONResponse({"error": "no transcript to edit"}, status_code=400)
        base_segments, *_rest, current_revision = base
        if base_revision != current_revision:
            return JSONResponse(
                {"error": "transcript changed; reload before saving"},
                status_code=409,
            )
        if not 0 <= idx < len(base_segments):
            return JSONResponse({"error": "segment index out of range"}, status_code=400)
        segments = copy.deepcopy(base_segments)
        # Word timings/text describe the raw ASR segment and become invalid once
        # its text is edited. Preserve segment timing/speaker, but clear stale words.
        segments[idx] = dict(segments[idx]) | {"text": text, "words": []}
        next_revision, expected_names = _create_transcript_revision(
            session, r, base_revision=base_revision, segments=segments,
            note=f"edited segment {idx}",
        )
    _queue_revision_reindex(file_id, next_revision, expected_names)
    return RedirectResponse(url=f"/file/{file_id}", status_code=303)


@app.post("/file/{file_id}/transcript/replace")
def replace_transcript_text(
    file_id: str,
    find: str = Form(...),
    replacement: str = Form(""),
    base_revision: int = Form(...),
    case_sensitive: bool = Form(False),
):
    """Literal transcript-wide find/replace as one atomic revision."""
    import copy
    import re

    if not find:
        return JSONResponse({"error": "find text must not be empty"}, status_code=400)
    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        base = _transcript_edit_base(r)
        if base is None:
            return JSONResponse({"error": "no transcript to edit"}, status_code=400)
        base_segments, *_rest, current_revision = base
        if base_revision != current_revision:
            return JSONResponse(
                {"error": "transcript changed; reload before saving"}, status_code=409
            )
        pattern = re.compile(re.escape(find), 0 if case_sensitive else re.IGNORECASE)
        segments = copy.deepcopy(base_segments)
        replacements = 0
        for segment in segments:
            updated, count = pattern.subn(replacement, segment.get("text") or "")
            if count:
                segment["text"] = updated
                segment["words"] = []
                replacements += count
        if replacements == 0:
            return JSONResponse({"error": "find text was not present"}, status_code=400)
        next_revision, expected_names = _create_transcript_revision(
            session, r, base_revision=base_revision, segments=segments,
            note=f"replaced {replacements} occurrence(s) of {find!r}",
        )
    _queue_revision_reindex(file_id, next_revision, expected_names)
    return RedirectResponse(url=f"/file/{file_id}", status_code=303)


@app.post("/api/files/{file_id}/transcript/bulk")
def api_bulk_edit_transcript(
    file_id: str, payload: TranscriptBulkEditInput
) -> JSONResponse:
    """Apply several explicit segment edits as one conflict-checked revision."""
    import copy

    if len({edit.index for edit in payload.edits}) != len(payload.edits):
        return JSONResponse({"error": "segment indices must be unique"}, status_code=400)
    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        base = _transcript_edit_base(r)
        if base is None:
            return JSONResponse({"error": "no transcript to edit"}, status_code=400)
        base_segments, *_rest, current_revision = base
        if payload.base_revision != current_revision:
            return JSONResponse(
                {"error": "transcript changed; reload before saving"}, status_code=409
            )
        if any(edit.index >= len(base_segments) for edit in payload.edits):
            return JSONResponse({"error": "segment index out of range"}, status_code=400)
        segments = copy.deepcopy(base_segments)
        for edit in payload.edits:
            segments[edit.index] = dict(segments[edit.index]) | {
                "text": edit.text, "words": [],
            }
        next_revision, expected_names = _create_transcript_revision(
            session, r, base_revision=payload.base_revision, segments=segments,
            note=f"bulk edited {len(payload.edits)} segment(s)",
        )
    _queue_revision_reindex(file_id, next_revision, expected_names)
    return JSONResponse({"file_id": file_id, "revision": next_revision,
                         "edited_segments": len(payload.edits)})


@app.get("/api/files/{file_id}/transcript/revisions")
def api_transcript_revisions(file_id: str) -> JSONResponse:
    with session_scope() as session:
        r = session.get(PlaudFile, file_id)
        if r is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        data = [{
            "id": revision.id, "revision": revision.revision,
            "source": revision.source, "kind": revision.kind, "note": revision.note,
            "base_transcript_id": revision.base_transcript_id,
            "created_at": revision.created_at.isoformat(),
            "dependent_summaries": session.query(Summary).filter_by(
                file_id=file_id, transcript_revision=revision.revision,
            ).count(),
            "dependent_chunks": session.query(Chunk).filter_by(
                file_id=file_id, transcript_revision=revision.revision,
            ).count(),
        } for revision in reversed(r.transcript_revisions)]
    return JSONResponse({"file_id": file_id, "revisions": data})


# --------------------------------------------------------------------------- #
# Settings page + API
# --------------------------------------------------------------------------- #


class SettingsInput(BaseModel):
    asr: dict[str, str]
    llm: dict[str, str]
    embeddings: dict[str, str]
    diarize: dict[str, str]
    align: dict[str, str]
    pipeline: dict
    poller: dict[str, int]


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    settings = get_settings()
    ctx = _base_ctx(request, "settings") | {
        "asr_providers": ["faster-whisper", "mlx-whisper", "whispercpp", "openai", "deepgram", "assemblyai", "remote-worker"],
        "llm_providers": ["ollama", "openai", "anthropic", "codex-local", "remote-worker"],
        "embeddings_providers": ["local", "openai", "ollama", "remote-worker"],
        "diarize_providers": ["pyannote", "remote-worker", "none"],
        "align_providers": ["whisper-timestamps", "whisperx", "remote-worker", "none"],
        "summary_templates": ["default", "meeting", "call", "lecture", "personal"],
        "pipeline_stages": [
            {"key": "convert", "label": "Convert (opus → wav)", "enabled": settings.pipeline.convert},
            {"key": "transcribe", "label": "Transcribe (ASR)", "enabled": settings.pipeline.transcribe},
            {"key": "align", "label": "Align (word-level timestamps)", "enabled": settings.pipeline.align},
            {"key": "diarize", "label": "Diarize (speaker detection)", "enabled": settings.pipeline.diarize},
            {"key": "correct", "label": "Correct (vocabulary/names)", "enabled": settings.pipeline.correct},
            {"key": "summarize", "label": "Summarize (notes)", "enabled": settings.pipeline.summarize},
            {"key": "mind_map", "label": "Mind map", "enabled": settings.pipeline.mind_map},
            {"key": "index", "label": "Index (embeddings)", "enabled": settings.pipeline.index},
        ],
        "cfg": {
            "asr_provider": settings.asr.provider,
            "asr_model": _get_asr_model(settings),
            "asr_language": settings.asr.language,
            "llm_provider": settings.llm.provider,
            "llm_model": _get_llm_model(settings),
            "embeddings_provider": settings.embeddings.provider,
            "embeddings_model": _get_embeddings_model(settings),
            "diarize_provider": settings.diarize.provider,
            "align_provider": settings.align.provider,
            "summary_template": settings.pipeline.summary_template,
            "poll_interval": settings.poller.interval_seconds,
        },
    }
    return templates.TemplateResponse(request=request, name="settings.html", context=ctx)


def _get_asr_model(settings) -> str:
    p = settings.asr.provider
    if p == "mlx-whisper":
        return settings.asr.mlx_whisper.model
    if p == "faster-whisper":
        return settings.asr.faster_whisper.model
    if p == "whispercpp":
        return settings.asr.whispercpp.model_path.name
    if p == "openai":
        return settings.asr.openai.model
    return ""


def _get_llm_model(settings) -> str:
    p = settings.llm.provider
    if p == "ollama":
        return settings.llm.ollama.model
    if p == "openai":
        return settings.llm.openai.model
    if p == "anthropic":
        return settings.llm.anthropic.model
    return ""


def _get_embeddings_model(settings) -> str:
    p = settings.embeddings.provider
    if p == "local":
        return settings.embeddings.local.model
    if p == "openai":
        return settings.embeddings.openai.model
    if p == "ollama":
        return settings.embeddings.ollama.model
    return ""


@app.post("/api/settings")
def api_save_settings(payload: SettingsInput) -> JSONResponse:
    import tomllib

    config_path = Path(os.environ.get("LOCALPLAUD_CONFIG", "config.toml"))
    if not config_path.exists():
        return JSONResponse({"error": "config.toml not found"}, status_code=404)

    # Read existing config
    with config_path.open("rb") as f:
        existing = tomllib.load(f)

    # Build new config sections
    new_config: dict = {}

    # ASR
    asr_cfg = existing.get("asr", {})
    asr_cfg["provider"] = payload.asr.get("provider", asr_cfg.get("provider"))
    asr_cfg["language"] = payload.asr.get("language", asr_cfg.get("language"))
    provider_key = payload.asr.get("provider", "").replace("-", "_")
    model = payload.asr.get("model", "")
    if model and provider_key in asr_cfg:
        asr_cfg[provider_key] = asr_cfg.get(provider_key, {})
        if provider_key == "whispercpp":
            asr_cfg[provider_key]["model_path"] = model
        else:
            asr_cfg[provider_key]["model"] = model
    new_config["asr"] = asr_cfg

    # LLM
    llm_cfg = existing.get("llm", {})
    llm_cfg["provider"] = payload.llm.get("provider", llm_cfg.get("provider"))
    llm_provider_key = payload.llm.get("provider", "")
    llm_model = payload.llm.get("model", "")
    if llm_model and llm_provider_key in llm_cfg:
        llm_cfg[llm_provider_key] = llm_cfg.get(llm_provider_key, {})
        llm_cfg[llm_provider_key]["model"] = llm_model
    new_config["llm"] = llm_cfg

    # Embeddings
    emb_cfg = existing.get("embeddings", {})
    emb_cfg["provider"] = payload.embeddings.get("provider", emb_cfg.get("provider"))
    emb_provider_key = payload.embeddings.get("provider", "")
    emb_model = payload.embeddings.get("model", "")
    if emb_model and emb_provider_key in emb_cfg:
        emb_cfg[emb_provider_key] = emb_cfg.get(emb_provider_key, {})
        emb_cfg[emb_provider_key]["model"] = emb_model
    new_config["embeddings"] = emb_cfg

    # Diarize
    dia_cfg = existing.get("diarize", {})
    dia_cfg["provider"] = payload.diarize.get("provider", dia_cfg.get("provider"))
    new_config["diarize"] = dia_cfg

    # Align
    align_cfg = existing.get("align", {})
    align_cfg["provider"] = payload.align.get("provider", align_cfg.get("provider"))
    new_config["align"] = align_cfg

    # Pipeline
    pipe_cfg = existing.get("pipeline", {})
    stages = payload.pipeline.get("stages", {})
    for key in ("convert", "transcribe", "align", "diarize", "correct", "summarize", "mind_map", "index"):
        if key in stages:
            pipe_cfg[key] = stages[key]
    if "summary_template" in payload.pipeline:
        pipe_cfg["summary_template"] = payload.pipeline["summary_template"]
    new_config["pipeline"] = pipe_cfg

    # Poller
    poller_cfg = existing.get("poller", {})
    if "interval_seconds" in payload.poller:
        poller_cfg["interval_seconds"] = payload.poller["interval_seconds"]
    new_config["poller"] = poller_cfg

    # Merge with existing (preserve sections not touched)
    merged = {**existing, **new_config}

    # Write back
    try:
        import tomli_w
        with config_path.open("w") as f:
            tomli_w.dump(merged, f)
    except ImportError:
        # Fallback: manual TOML write for simple values
        lines = []
        for section, values in merged.items():
            lines.append(f"[{section}]")
            for k, v in values.items():
                if isinstance(v, dict):
                    lines.append(f"  [{section}.{k}]")
                    for dk, dv in v.items():
                        lines.append(f'  {dk} = {_toml_value(dv)}')
                else:
                    lines.append(f'{k} = {_toml_value(v)}')
            lines.append("")
        with config_path.open("w") as f:
            f.write("\n".join(lines))

    return JSONResponse({"ok": True, "message": "Settings saved. Restart required."})


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, list):
        items = ", ".join(_toml_value(x) for x in v)
        return f"[{items}]"
    return str(v)
