"""Reference server and durable job store for the localplaud worker protocol.

This module deliberately has no dependency on the Plaud client or application
database.  A remote worker receives only the minimal :class:`JobInput` described
by the protocol and persists enough state to make submission retries safe across
process restarts.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .remote_worker import (
    PROTOCOL_VERSION,
    ArtifactManifest,
    StructuredWorkerError,
    WorkerCapability,
    WorkerHandshake,
    WorkerJobRequest,
    WorkerJobStatus,
    artifact_checksum,
    verify_signature,
)


class JobConflictError(ValueError):
    """An idempotency key or job id was reused for different work."""


class JobNotFoundError(LookupError):
    """The requested job does not exist."""


class InvalidTransitionError(ValueError):
    """The requested durable job transition is not allowed."""


_TERMINAL_STATES = {"completed", "failed", "cancelled"}


class RemoteWorkerJobStore:
    """SQLite persistence for idempotent remote jobs.

    A new connection is used for every transaction.  SQLite's unique constraint
    on ``idempotency_key`` is the final concurrency guard; ``BEGIN IMMEDIATE``
    makes check-and-insert atomic between worker threads and processes.
    """

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._claims: dict[str, str] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._schema_lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS remote_worker_jobs (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    progress REAL NOT NULL,
                    artifact_json TEXT,
                    error_json TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    claim_token TEXT,
                    lease_until REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(remote_worker_jobs)"
            ).fetchall()}
            if "claim_token" not in columns:
                connection.execute("ALTER TABLE remote_worker_jobs ADD COLUMN claim_token TEXT")
            if "lease_until" not in columns:
                connection.execute("ALTER TABLE remote_worker_jobs ADD COLUMN lease_until REAL")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _canonical_request(request: WorkerJobRequest) -> tuple[str, str]:
        # job_id is an attempt identifier, not part of the logical idempotent work.
        value = request.model_dump(mode="json", exclude={"job_id"})
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return encoded, hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _status(row: sqlite3.Row) -> WorkerJobStatus:
        artifact = json.loads(row["artifact_json"]) if row["artifact_json"] else None
        error = json.loads(row["error_json"]) if row["error_json"] else None
        return WorkerJobStatus(
            job_id=row["job_id"],
            idempotency_key=row["idempotency_key"],
            state=row["state"],
            progress=row["progress"],
            artifact=artifact,
            error=error,
        )

    def submit(self, request: WorkerJobRequest) -> WorkerJobStatus:
        request_json, request_hash = self._canonical_request(request)
        now = time.time()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM remote_worker_jobs WHERE idempotency_key = ? OR job_id = ?",
                (request.idempotency_key, request.job_id),
            ).fetchone()
            if existing is not None:
                if (existing["idempotency_key"] != request.idempotency_key
                        or existing["request_hash"] != request_hash):
                    raise JobConflictError("job identity was reused with a different request")
                error = json.loads(existing["error_json"]) if existing["error_json"] else None
                if existing["state"] == "failed" and error and error.get("retryable"):
                    connection.execute(
                        """UPDATE remote_worker_jobs
                           SET state = 'queued', progress = 0, error_json = NULL,
                               artifact_json = NULL, cancel_requested = 0,
                               claim_token = NULL, lease_until = NULL, updated_at = ?
                           WHERE job_id = ? AND state = 'failed'""",
                        (now, existing["job_id"]),
                    )
                    existing = connection.execute(
                        "SELECT * FROM remote_worker_jobs WHERE job_id = ?",
                        (existing["job_id"],),
                    ).fetchone()
                return self._status(existing)
            connection.execute(
                """INSERT INTO remote_worker_jobs
                   (job_id, idempotency_key, request_hash, request_json, state, progress,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'queued', 0, ?, ?)""",
                (request.job_id, request.idempotency_key, request_hash, request_json, now, now),
            )
            row = connection.execute(
                "SELECT * FROM remote_worker_jobs WHERE job_id = ?", (request.job_id,)
            ).fetchone()
            assert row is not None
            return self._status(row)

    def get(self, job_id: str) -> WorkerJobStatus:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM remote_worker_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return self._status(row)

    def queue_state(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, count(*) AS n FROM remote_worker_jobs GROUP BY state"
            ).fetchall()
        counts = {row["state"]: row["n"] for row in rows}
        return {"queued": counts.get("queued", 0), "running": counts.get("running", 0)}

    def request_payload(self, job_id: str) -> WorkerJobRequest:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_id, request_json FROM remote_worker_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        data = json.loads(row["request_json"])
        data["job_id"] = row["job_id"]
        return WorkerJobRequest.model_validate(data)

    def claim_next(self, capabilities: set[str] | None = None,
                   *, lease_seconds: float = 120) -> WorkerJobRequest | None:
        """Atomically claim queued work or reclaim an expired crashed-worker lease."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._transaction() as connection:
            now = time.time()
            rows = connection.execute(
                """SELECT * FROM remote_worker_jobs
                   WHERE state = 'queued' OR
                         (state = 'running' AND (lease_until IS NULL OR lease_until <= ?))
                   ORDER BY created_at""",
                (now,),
            ).fetchall()
            for row in rows:
                data = json.loads(row["request_json"])
                if capabilities is not None and data.get("capability") not in capabilities:
                    continue
                claim_token = uuid.uuid4().hex
                updated = connection.execute(
                    """UPDATE remote_worker_jobs
                       SET state = 'running', claim_token = ?, lease_until = ?, updated_at = ?
                       WHERE job_id = ? AND
                         (state = 'queued' OR (state = 'running' AND
                          (lease_until IS NULL OR lease_until <= ?)))""",
                    (claim_token, now + lease_seconds, now, row["job_id"], now),
                )
                if updated.rowcount != 1:
                    continue
                self._claims[row["job_id"]] = claim_token
                data["job_id"] = row["job_id"]
                return WorkerJobRequest.model_validate(data)
        return None

    def renew_claim(self, job_id: str, *, lease_seconds: float = 120) -> None:
        token = self._claims.get(job_id)
        if token is None:
            raise InvalidTransitionError("this runner does not own the job claim")
        now = time.time()
        with self._transaction() as connection:
            updated = connection.execute(
                """UPDATE remote_worker_jobs SET lease_until = ?, updated_at = ?
                   WHERE job_id = ? AND state = 'running' AND claim_token = ?""",
                (now + lease_seconds, now, job_id, token),
            )
            if updated.rowcount != 1:
                raise InvalidTransitionError("job claim was lost")

    def start(self, job_id: str) -> WorkerJobStatus:
        return self._transition(job_id, "running", progress=0)

    def progress(self, job_id: str, progress: float) -> WorkerJobStatus:
        if not 0 <= progress <= 1:
            raise ValueError("progress must be between zero and one")
        return self._transition(job_id, "running", progress=progress)

    def complete(self, job_id: str, payload: bytes, *, content_type: str,
                 download_url: str | None = None,
                 inline_payload: dict[str, Any] | None = None) -> WorkerJobStatus:
        artifact = ArtifactManifest(
            content_type=content_type,
            size_bytes=len(payload),
            sha256=artifact_checksum(payload),
            download_url=download_url,
            payload=inline_payload,
        )
        return self._transition(job_id, "completed", progress=1, artifact=artifact)

    def fail(self, job_id: str, error: StructuredWorkerError) -> WorkerJobStatus:
        return self._transition(job_id, "failed", error=error)

    def cancel(self, job_id: str) -> WorkerJobStatus:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM remote_worker_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            if row["state"] == "completed" or row["state"] == "failed":
                return self._status(row)
            if row["state"] != "cancelled":
                connection.execute(
                    """UPDATE remote_worker_jobs SET state = 'cancelled', cancel_requested = 1,
                       claim_token = NULL, lease_until = NULL, updated_at = ?
                       WHERE job_id = ?""", (time.time(), job_id)
                )
                row = connection.execute(
                    "SELECT * FROM remote_worker_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            assert row is not None
            if row["state"] == "cancelled":
                self._claims.pop(job_id, None)
            return self._status(row)

    def cancellation_requested(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM remote_worker_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return bool(row["cancel_requested"])

    def _transition(self, job_id: str, state: str, *, progress: float | None = None,
                    artifact: ArtifactManifest | None = None,
                    error: StructuredWorkerError | None = None) -> WorkerJobStatus:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM remote_worker_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            current = row["state"]
            owned_token = self._claims.get(job_id)
            if owned_token is not None and row["claim_token"] != owned_token:
                raise InvalidTransitionError("job claim was lost")
            if current in _TERMINAL_STATES:
                # A completed result is immutable and safe to rediscover on retry.
                if current == state:
                    return self._status(row)
                raise InvalidTransitionError(f"cannot transition {current} job to {state}")
            if state == "running" and current not in {"queued", "running"}:
                raise InvalidTransitionError(f"cannot transition {current} job to running")
            next_progress = row["progress"] if progress is None else progress
            if next_progress < row["progress"]:
                raise InvalidTransitionError("job progress cannot move backwards")
            connection.execute(
                """UPDATE remote_worker_jobs
                   SET state = ?, progress = ?, artifact_json = ?, error_json = ?,
                       claim_token = ?, lease_until = ?, updated_at = ?
                   WHERE job_id = ?""",
                (state, next_progress,
                 artifact.model_dump_json() if artifact else None,
                 error.model_dump_json() if error else None,
                 None if state in _TERMINAL_STATES else row["claim_token"],
                 None if state in _TERMINAL_STATES else row["lease_until"],
                 time.time(), job_id),
            )
            updated = connection.execute(
                "SELECT * FROM remote_worker_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            assert updated is not None
            if state in _TERMINAL_STATES:
                self._claims.pop(job_id, None)
            return self._status(updated)


def _error(code: str, message: str, status_code: int, *, retryable: bool = False,
           detail: dict[str, Any] | None = None) -> JSONResponse:
    error = StructuredWorkerError(
        code=code, message=message, retryable=retryable, detail=detail or {}
    )
    return JSONResponse(status_code=status_code, content={"error": error.model_dump(mode="json")})


def _contains_plaud_credentials(value: Any, *, key: str = "") -> bool:
    """Reject any credential-shaped job data; workers use only their own secrets."""
    normalized = key.lower().replace("-", "_")
    credential_keys = {
        "token", "access_token", "refresh_token", "api_key", "cookie",
        "authorization", "credential", "credentials", "secret", "password",
    }
    credential_suffixes = (
        "_access_token", "_refresh_token", "_api_key", "_cookie",
        "_authorization", "_credential", "_credentials", "_secret", "_password",
    )
    if normalized in credential_keys or normalized.endswith(credential_suffixes):
        return True
    if isinstance(value, dict):
        return any(_contains_plaud_credentials(item, key=str(name))
                   for name, item in value.items())
    if isinstance(value, list):
        return any(_contains_plaud_credentials(item, key=key) for item in value)
    return False


def create_remote_worker_app(*, token: str, worker_id: str,
                             capabilities: Sequence[WorkerCapability],
                             store: RemoteWorkerJobStore,
                             max_concurrent_jobs: int = 1,
                             runtime: dict[str, Any] | None = None) -> FastAPI:
    """Create an authenticated protocol-v1 worker API.

    Execution is intentionally decoupled from HTTP: a GPU runner consumes queued
    requests through ``store.request_payload`` and records progress/results through
    the store.  This keeps acknowledgements fast and jobs durable.
    """
    if not token:
        raise ValueError("worker authentication token must not be empty")
    capability_map = {item.name: item for item in capabilities}
    app = FastAPI(title="localplaud remote worker", docs_url=None, redoc_url=None)
    app.state.job_store = store

    @app.middleware("http")
    async def authenticate(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.headers.get("X-Localplaud-Protocol") != PROTOCOL_VERSION:
            return _error("protocol_mismatch", "unsupported worker protocol", 426)
        body = await request.body()
        timestamp = request.headers.get("X-Localplaud-Timestamp", "")
        signature = request.headers.get("X-Localplaud-Signature", "")
        if not verify_signature(token, request.method, request.url.path, body,
                                timestamp, signature):
            return _error("authentication_failed", "invalid or expired request signature", 401)
        return await call_next(request)

    @app.get("/v1/handshake", response_model=WorkerHandshake)
    def handshake() -> WorkerHandshake:
        return WorkerHandshake(
            protocol_version=PROTOCOL_VERSION,
            worker_id=worker_id,
            capabilities=list(capabilities),
            max_concurrent_jobs=max_concurrent_jobs,
            runtime={**(runtime or {}), "queue": store.queue_state()},
        )

    @app.post("/v1/jobs", response_model=WorkerJobStatus)
    async def submit(request: Request):  # type: ignore[no-untyped-def]
        try:
            raw_job = await request.json()
            if _contains_plaud_credentials(raw_job):
                return _error("forbidden_credentials",
                              "Plaud account credentials must not be sent to a worker", 422)
            job = WorkerJobRequest.model_validate(raw_job)
        except Exception as exc:
            return _error("invalid_request", "invalid worker job request", 422,
                          detail={"reason": str(exc)})
        if job.protocol_version != PROTOCOL_VERSION:
            return _error("protocol_mismatch", "unsupported job protocol", 426)
        capability = capability_map.get(job.capability)
        if capability is None:
            return _error("unsupported_capability", f"unsupported capability: {job.capability}", 422)
        if job.model not in capability.models:
            return _error("unsupported_model", f"unsupported model: {job.model}", 422)
        try:
            return store.submit(job)
        except JobConflictError as exc:
            return _error("idempotency_conflict", str(exc), 409)

    @app.get("/v1/jobs/{job_id}", response_model=WorkerJobStatus)
    def status(job_id: str):  # type: ignore[no-untyped-def]
        try:
            return store.get(job_id)
        except JobNotFoundError:
            return _error("job_not_found", "job does not exist", 404)

    @app.post("/v1/jobs/{job_id}/cancel", response_model=WorkerJobStatus)
    def cancel(job_id: str):  # type: ignore[no-untyped-def]
        try:
            return store.cancel(job_id)
        except JobNotFoundError:
            return _error("job_not_found", "job does not exist", 404)

    return app
