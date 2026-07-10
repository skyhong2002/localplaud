"""Versioned protocol client and wire models for trusted remote GPU workers.

Workers receive a short-lived audio fetch URL or an upload reference, never Plaud
OAuth credentials. Job and artifact identifiers make retries idempotent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = "1"


class WorkerCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    models: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)


class WorkerHandshake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: str
    worker_id: str
    capabilities: list[WorkerCapability]
    max_concurrent_jobs: int = 1
    runtime: dict[str, Any] = Field(default_factory=dict)


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["signed_url", "upload_ref"]
    location: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    expires_at: int | None = None


class WorkerJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: str = PROTOCOL_VERSION
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    idempotency_key: str
    capability: str
    model: str
    input: JobInput | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class StructuredWorkerError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    retryable: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class ArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    download_url: str | None = None
    payload: dict[str, Any] | None = None


class WorkerJobStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    idempotency_key: str
    state: Literal["queued", "running", "completed", "failed", "cancelled"]
    progress: float = Field(ge=0, le=1)
    artifact: ArtifactManifest | None = None
    error: StructuredWorkerError | None = None


def artifact_checksum(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def signed_headers(token: str, method: str, path: str, body: bytes = b"") -> dict[str, str]:
    timestamp = str(int(time.time()))
    digest = hashlib.sha256(body).hexdigest()
    message = f"{timestamp}\n{method.upper()}\n{path}\n{digest}".encode()
    signature = hmac.new(token.encode(), message, hashlib.sha256).hexdigest()
    return {"X-Localplaud-Timestamp": timestamp, "X-Localplaud-Signature": signature,
            "X-Localplaud-Protocol": PROTOCOL_VERSION}


def verify_signature(token: str, method: str, path: str, body: bytes,
                     timestamp: str, signature: str, *, max_age_seconds: int = 300) -> bool:
    try:
        age = abs(int(time.time()) - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > max_age_seconds:
        return False
    expected = signed_headers_at(token, method, path, body, timestamp)
    return hmac.compare_digest(signature, expected)


def signed_headers_at(token: str, method: str, path: str, body: bytes, timestamp: str) -> str:
    digest = hashlib.sha256(body).hexdigest()
    message = f"{timestamp}\n{method.upper()}\n{path}\n{digest}".encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


class RemoteWorkerClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)

    def _request(self, method: str, path: str, payload: dict | None = None) -> httpx.Response:
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
        response = self.client.request(method, path, content=body,
                                       headers={"Content-Type": "application/json",
                                                **signed_headers(self.token, method, path, body)})
        response.raise_for_status()
        return response

    def handshake(self) -> WorkerHandshake:
        result = WorkerHandshake.model_validate(self._request("GET", "/v1/handshake").json())
        if result.protocol_version != PROTOCOL_VERSION:
            raise RuntimeError(f"worker protocol mismatch: {result.protocol_version}")
        return result

    def submit(self, request: WorkerJobRequest) -> WorkerJobStatus:
        return WorkerJobStatus.model_validate(
            self._request("POST", "/v1/jobs", request.model_dump(mode="json")).json()
        )

    def status(self, job_id: str) -> WorkerJobStatus:
        return WorkerJobStatus.model_validate(self._request("GET", f"/v1/jobs/{job_id}").json())

    def cancel(self, job_id: str) -> WorkerJobStatus:
        return WorkerJobStatus.model_validate(
            self._request("POST", f"/v1/jobs/{job_id}/cancel", {}).json()
        )

    def close(self) -> None:
        self.client.close()
