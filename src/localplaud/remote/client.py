"""Controller client for authenticated localplaud-worker v1 endpoints."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from .protocol import HandshakeResponse, JobResponse, JobStatus, JobSubmitRequest


class RemoteWorkerError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ArtifactChecksumError(RemoteWorkerError):
    pass


@dataclass
class RemoteResult:
    job: JobResponse
    artifacts: dict[str, bytes]


class RemoteWorkerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/") + "/"
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_config(cls, config: dict):
        token_env = config.get("token_env", "LOCALPLAUD_WORKER_TOKEN")
        token = os.environ.get(token_env)
        if not token:
            raise RemoteWorkerError(f"worker token environment variable is missing: {token_env}")
        return cls(
            config["base_url"],
            token,
            timeout=float(config.get("timeout", 120)),
        )

    def close(self):
        self._client.close()

    def handshake(self) -> HandshakeResponse:
        response = self._client.get("api/worker/v1/capabilities")
        response.raise_for_status()
        return HandshakeResponse.model_validate(response.json())

    def submit(self, request: JobSubmitRequest) -> JobResponse:
        response = self._client.post("api/worker/v1/jobs", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return JobResponse.model_validate(response.json())

    def status(self, job_id: str) -> JobResponse:
        response = self._client.get(f"api/worker/v1/jobs/{job_id}")
        response.raise_for_status()
        return JobResponse.model_validate(response.json())

    def cancel(self, job_id: str) -> None:
        response = self._client.post(f"api/worker/v1/jobs/{job_id}/cancel")
        response.raise_for_status()

    def wait(
        self,
        job: JobResponse,
        *,
        timeout: float = 600,
        initial_backoff: float = 0.2,
        max_backoff: float = 3.0,
    ) -> RemoteResult:
        deadline = time.monotonic() + timeout
        backoff = initial_backoff
        while job.status in {JobStatus.queued, JobStatus.running}:
            if time.monotonic() >= deadline:
                raise RemoteWorkerError("remote worker timed out", retryable=True)
            time.sleep(backoff)
            backoff = min(max_backoff, backoff * 1.7)
            job = self.status(job.job_id)
        if job.status != JobStatus.succeeded:
            error = job.error
            raise RemoteWorkerError(
                error.message if error else f"remote job ended as {job.status}",
                retryable=bool(error and error.retryable),
            )
        artifacts: dict[str, bytes] = {}
        for descriptor in job.artifacts:
            url = urljoin(self.base_url, descriptor.download_url.lstrip("/"))
            response = self._client.get(url)
            response.raise_for_status()
            data = response.content
            digest = hashlib.sha256(data).hexdigest()
            if not hmac.compare_digest(digest, descriptor.sha256):
                raise ArtifactChecksumError(f"artifact checksum mismatch: {descriptor.name}")
            artifacts[descriptor.name] = data
        return RemoteResult(job=job, artifacts=artifacts)

    def submit_and_wait(self, request: JobSubmitRequest, **wait_options) -> RemoteResult:
        return self.wait(self.submit(request), **wait_options)
