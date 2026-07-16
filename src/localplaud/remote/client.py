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

MAX_PROVIDER_TIMEOUT_SECONDS = 23 * 60 * 60


def validate_provider_timeout(value: object, *, field: str) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not 0 < timeout <= MAX_PROVIDER_TIMEOUT_SECONDS:
        raise ValueError(
            f"{field} must be greater than zero and no more than "
            f"{MAX_PROVIDER_TIMEOUT_SECONDS} seconds"
        )
    return timeout


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
        self._request_timeout = validate_provider_timeout(timeout, field="timeout")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"authorization": f"Bearer {token}"},
            timeout=self._request_timeout,
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
            timeout=validate_provider_timeout(config.get("timeout", 120), field="timeout"),
        )

    def close(self):
        self._client.close()

    def handshake(self) -> HandshakeResponse:
        response = self._client.get("api/worker/v1/capabilities")
        response.raise_for_status()
        return HandshakeResponse.model_validate(response.json())

    def submit(self, request: JobSubmitRequest, *, timeout: float | None = None) -> JobResponse:
        response = self._client.post(
            "api/worker/v1/jobs",
            json=request.model_dump(mode="json"),
            timeout=self._request_timeout if timeout is None else timeout,
        )
        response.raise_for_status()
        return JobResponse.model_validate(response.json())

    def status(self, job_id: str, *, timeout: float | None = None) -> JobResponse:
        response = self._client.get(
            f"api/worker/v1/jobs/{job_id}",
            timeout=self._request_timeout if timeout is None else timeout,
        )
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
        _deadline: float | None = None,
    ) -> RemoteResult:
        timeout = validate_provider_timeout(timeout, field="job_timeout")
        deadline = _deadline if _deadline is not None else time.monotonic() + timeout
        backoff = initial_backoff

        def remaining_timeout() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteWorkerError("remote worker timed out", retryable=True)
            return min(self._request_timeout, remaining)

        while job.status in {JobStatus.queued, JobStatus.running}:
            remaining = remaining_timeout()
            time.sleep(min(backoff, remaining))
            backoff = min(max_backoff, backoff * 1.7)
            job = self.status(job.job_id, timeout=remaining_timeout())
            remaining_timeout()
        if job.status != JobStatus.succeeded:
            error = job.error
            raise RemoteWorkerError(
                error.message if error else f"remote job ended as {job.status}",
                retryable=bool(error and error.retryable),
            )
        artifacts: dict[str, bytes] = {}
        for descriptor in job.artifacts:
            url = urljoin(self.base_url, descriptor.download_url.lstrip("/"))
            response = self._client.get(url, timeout=remaining_timeout())
            response.raise_for_status()
            remaining_timeout()
            data = response.content
            digest = hashlib.sha256(data).hexdigest()
            if not hmac.compare_digest(digest, descriptor.sha256):
                raise ArtifactChecksumError(f"artifact checksum mismatch: {descriptor.name}")
            artifacts[descriptor.name] = data
        return RemoteResult(job=job, artifacts=artifacts)

    def submit_and_wait(self, request: JobSubmitRequest, **wait_options) -> RemoteResult:
        timeout = validate_provider_timeout(
            wait_options.pop("timeout", 600), field="job_timeout"
        )
        deadline = time.monotonic() + timeout
        submit_timeout = min(self._request_timeout, max(0.0, deadline - time.monotonic()))
        if submit_timeout <= 0:
            raise RemoteWorkerError("remote worker timed out", retryable=True)
        job = self.submit(request, timeout=submit_timeout)
        if time.monotonic() >= deadline:
            raise RemoteWorkerError("remote worker timed out", retryable=True)
        return self.wait(job, timeout=timeout, _deadline=deadline, **wait_options)
