"""Pydantic wire contract for localplaud-worker protocol v1."""

from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_NAME = "localplaud-worker"
PROTOCOL_VERSION = "1"


class JobStage(enum.StrEnum):
    transcribe = "transcribe"
    diarize = "diarize"
    summarize = "summarize"
    mind_map = "mind_map"
    embed = "embed"


class JobStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class WorkerError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class InputReference(BaseModel):
    name: str
    media_type: str
    kind: Literal["inline_json", "inline_base64", "url"]
    value: Any
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ArtifactDescriptor(BaseModel):
    name: str
    media_type: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    download_url: str


class StageCapability(BaseModel):
    stage: JobStage
    models: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HandshakeResponse(BaseModel):
    protocol: Literal["localplaud-worker"] = PROTOCOL_NAME
    version: Literal["1"] = PROTOCOL_VERSION
    worker_id: str
    capabilities: list[StageCapability]


_FORBIDDEN = {
    "oauth", "authorization", "access_token", "refresh_token", "plaud_token",
    "plaud_credentials", "cookie", "api_key",
}


def _contains_credentials(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in _FORBIDDEN or _contains_credentials(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_credentials(item) for item in value)
    return False


class JobSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    idempotency_key: str = Field(min_length=1, max_length=256)
    stage: JobStage
    model: str | None = None
    inputs: list[InputReference] = Field(min_length=1)
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_credentials(self):
        if _contains_credentials(self.model_dump()):
            raise ValueError("credentials are forbidden in remote worker jobs")
        return self


class JobResponse(BaseModel):
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    job_id: str
    idempotency_key: str
    stage: JobStage
    status: JobStatus
    progress: float = Field(ge=0, le=1)
    artifacts: list[ArtifactDescriptor] = Field(default_factory=list)
    error: WorkerError | None = None


class CancelResponse(BaseModel):
    job_id: str
    status: JobStatus
