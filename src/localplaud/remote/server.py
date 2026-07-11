"""Authenticated durable localplaud-worker v1 API and stage executor."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..asr.base import Segment, Transcript, Word
from ..config import get_settings
from ..db.models import RemoteJob
from ..db.session import session_scope
from ..plaud.client import _assert_safe_fetch_url
from .protocol import (
    ArtifactDescriptor,
    CancelResponse,
    HandshakeResponse,
    InputReference,
    JobResponse,
    JobStage,
    JobStatus,
    JobSubmitRequest,
    StageCapability,
    WorkerError,
)

router = APIRouter(prefix="/api/worker/v1", tags=["remote-worker"])
_bearer = HTTPBearer(auto_error=False)


def _authorize(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),  # noqa: B008
) -> None:
    expected = os.environ.get("LOCALPLAUD_WORKER_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="remote worker token is not configured")
    supplied = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid worker token")


def _descriptor(job_id: str, artifact: dict) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        name=artifact["name"],
        media_type=artifact["media_type"],
        size=artifact["size"],
        sha256=artifact["sha256"],
        download_url=f"/api/worker/v1/jobs/{job_id}/artifacts/{artifact['name']}",
    )


def _response(row: RemoteJob) -> JobResponse:
    return JobResponse(
        job_id=row.id,
        idempotency_key=row.idempotency_key,
        stage=row.stage,
        status=row.status,
        progress=row.progress,
        artifacts=[_descriptor(row.id, item) for item in row.artifacts or []],
        error=row.error,
    )


def _decode_input(ref: InputReference) -> bytes:
    if ref.kind == "inline_json":
        data = json.dumps(ref.value, ensure_ascii=False, separators=(",", ":")).encode()
    elif ref.kind == "inline_base64":
        data = base64.b64decode(str(ref.value), validate=True)
    else:
        _assert_safe_fetch_url(str(ref.value))
        with httpx.Client(timeout=120, follow_redirects=False) as client:
            response = client.get(str(ref.value))
            response.raise_for_status()
            data = response.content
    if ref.sha256 and not hmac.compare_digest(hashlib.sha256(data).hexdigest(), ref.sha256):
        raise ValueError(f"input checksum mismatch: {ref.name}")
    return data


def _transcript(value: bytes) -> Transcript:
    payload = json.loads(value)
    segments = []
    for item in payload.get("segments", []):
        words = [Word(**word) for word in item.get("words", [])]
        segments.append(Segment(**(item | {"words": words})))
    return Transcript(
        segments=segments,
        language=payload.get("language"),
        duration=payload.get("duration"),
        provider=payload.get("provider", "remote"),
        model=payload.get("model"),
        has_speakers=payload.get("has_speakers", False),
    )


def _artifact(name: str, media_type: str, data: bytes) -> dict:
    return {
        "name": name,
        "media_type": media_type,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data_base64": base64.b64encode(data).decode(),
    }


def _execute(request: JobSubmitRequest) -> list[dict]:
    inputs = {item.name: _decode_input(item) for item in request.inputs}
    settings = get_settings().model_copy(deep=True)
    if request.model:
        if request.stage == JobStage.transcribe:
            cfg = getattr(settings.asr, settings.asr.provider.replace("-", "_"))
            if hasattr(cfg, "model"):
                cfg.model = request.model
        elif request.stage == JobStage.diarize:
            settings.diarize.model = request.model
        elif request.stage in {JobStage.summarize, JobStage.mind_map}:
            cfg = getattr(settings.llm, settings.llm.provider.replace("-", "_"))
            if hasattr(cfg, "model"):
                cfg.model = request.model
        elif request.stage == JobStage.embed:
            cfg = getattr(
                settings.embeddings, settings.embeddings.provider.replace("-", "_")
            )
            if hasattr(cfg, "model"):
                cfg.model = request.model
    if request.stage == JobStage.transcribe:
        from ..worker.transcribe import run_asr, segments_to_json

        suffix = request.options.get("suffix", ".wav")
        with tempfile.NamedTemporaryFile(suffix=suffix) as audio:
            audio.write(inputs["audio"])
            audio.flush()
            transcript = run_asr(Path(audio.name), settings)
        payload = {
            "segments": segments_to_json(transcript),
            "language": transcript.language,
            "duration": transcript.duration,
            "provider": transcript.provider,
            "model": transcript.model,
            "has_speakers": transcript.has_speakers,
        }
        return [_artifact("transcript.json", "application/json", json.dumps(payload).encode())]
    transcript = _transcript(inputs["transcript"])
    if request.stage == JobStage.diarize:
        from ..worker.diarize import diarize
        from ..worker.transcribe import segments_to_json

        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            audio.write(inputs["audio"])
            audio.flush()
            result = diarize(Path(audio.name), transcript, settings.diarize)
        payload = {"segments": segments_to_json(result), "has_speakers": result.has_speakers}
    elif request.stage == JobStage.summarize:
        from ..worker.summarize import summarize

        payload = summarize(transcript, settings, request.options.get("template"))
    elif request.stage == JobStage.mind_map:
        from ..worker.mindmap import generate_mind_map

        payload = generate_mind_map(transcript, settings, request.options.get("summary_md"))
    elif request.stage == JobStage.embed:
        from ..worker.index import build_chunks, embed_chunks

        chunks = build_chunks(transcript)
        blobs, model, dim = embed_chunks(chunks, settings)
        payload = {
            "chunks": chunks,
            "vectors_base64": [base64.b64encode(blob).decode() for blob in blobs],
            "model": model,
            "dim": dim,
        }
    else:  # pragma: no cover - enum keeps this unreachable
        raise ValueError(f"unsupported stage: {request.stage}")
    return [_artifact("result.json", "application/json", json.dumps(payload).encode())]


def execute_job(job_id: str) -> None:
    with session_scope() as session:
        row = session.get(RemoteJob, job_id)
        if row is None or row.status not in {JobStatus.queued, JobStatus.running}:
            return
        if row.cancel_requested:
            row.status, row.progress = JobStatus.cancelled, 1.0
            row.completed_at = datetime.now(UTC)
            return
        row.status, row.progress = JobStatus.running, 0.05
        row.started_at = row.started_at or datetime.now(UTC)
        row.attempts += 1
        request = JobSubmitRequest.model_validate(row.input_manifest)
    try:
        artifacts = _execute(request)
        with session_scope() as session:
            row = session.get(RemoteJob, job_id)
            if row.cancel_requested:
                row.status, row.progress = JobStatus.cancelled, 1.0
            else:
                row.artifacts = artifacts
                row.status, row.progress = JobStatus.succeeded, 1.0
            row.completed_at = datetime.now(UTC)
    except Exception as exc:  # noqa: BLE001 - persisted structured worker failure
        with session_scope() as session:
            row = session.get(RemoteJob, job_id)
            row.status, row.progress = JobStatus.failed, 1.0
            row.error = WorkerError(
                code="stage_execution_failed",
                message=str(exc),
                retryable=isinstance(exc, (httpx.TimeoutException, OSError)),
            ).model_dump(mode="json")
            row.completed_at = datetime.now(UTC)


def resume_pending_jobs() -> None:
    with session_scope() as session:
        ids = list(
            session.scalars(
                select(RemoteJob.id).where(RemoteJob.status.in_([JobStatus.queued, JobStatus.running]))
            )
        )
    for job_id in ids:
        execute_job(job_id)


@router.get("/capabilities", response_model=HandshakeResponse, dependencies=[Depends(_authorize)])
def capabilities():
    settings = get_settings()
    return HandshakeResponse(
        worker_id=os.environ.get("LOCALPLAUD_WORKER_ID", "local-worker"),
        capabilities=[
            StageCapability(stage="transcribe", models=[getattr(getattr(settings.asr, settings.asr.provider.replace('-', '_')), "model", settings.asr.provider)]),
            StageCapability(stage="diarize", models=[settings.diarize.model]),
            StageCapability(stage="summarize", models=[getattr(getattr(settings.llm, settings.llm.provider), "model", settings.llm.provider)]),
            StageCapability(stage="mind_map", models=[getattr(getattr(settings.llm, settings.llm.provider), "model", settings.llm.provider)]),
            StageCapability(stage="embed", models=[getattr(getattr(settings.embeddings, settings.embeddings.provider), "model", settings.embeddings.provider)]),
        ],
    )


@router.post("/jobs", response_model=JobResponse, dependencies=[Depends(_authorize)])
def submit_job(request: JobSubmitRequest, background: BackgroundTasks):
    with session_scope() as session:
        existing = session.scalar(
            select(RemoteJob).where(RemoteJob.idempotency_key == request.idempotency_key)
        )
        if existing is not None:
            return _response(existing)
        row = RemoteJob(
            id=uuid.uuid4().hex,
            idempotency_key=request.idempotency_key,
            stage=request.stage,
            model=request.model,
            input_manifest=request.model_dump(mode="json"),
            options=request.options,
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            existing = session.scalar(
                select(RemoteJob).where(
                    RemoteJob.idempotency_key == request.idempotency_key
                )
            )
            if existing is None:
                raise
            return _response(existing)
        response = _response(row)
        job_id = row.id
    background.add_task(execute_job, job_id)
    return response


@router.get("/jobs/{job_id}", response_model=JobResponse, dependencies=[Depends(_authorize)])
def job_status(job_id: str):
    with session_scope() as session:
        row = session.get(RemoteJob, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _response(row)


@router.post("/jobs/{job_id}/cancel", response_model=CancelResponse, dependencies=[Depends(_authorize)])
def cancel_job(job_id: str):
    with session_scope() as session:
        row = session.get(RemoteJob, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        if row.status in {JobStatus.queued, JobStatus.running}:
            row.cancel_requested = True
            if row.status == JobStatus.queued:
                row.status, row.progress = JobStatus.cancelled, 1.0
        return CancelResponse(job_id=row.id, status=row.status)


@router.get("/jobs/{job_id}/artifacts/{name}", dependencies=[Depends(_authorize)])
def download_artifact(job_id: str, name: str):
    with session_scope() as session:
        row = session.get(RemoteJob, job_id)
        artifact = next((item for item in (row.artifacts if row else []) if item["name"] == name), None)
        if artifact is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        data = base64.b64decode(artifact["data_base64"])
        return Response(data, media_type=artifact["media_type"])
