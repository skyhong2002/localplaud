"""Shared authenticated JSON inference over remote-worker protocol v1."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import quote

from ..asr.remote_worker_provider import sign_input
from .failures import ProviderUnavailableError, RetryableProviderError
from .remote_worker import JobInput, RemoteWorkerClient, WorkerJobRequest


def run_remote_json(cfg, capability: str, options: dict) -> dict:
    if not cfg.base_url or not cfg.token or not cfg.model:
        raise RuntimeError("remote worker base_url, token, and model are required")
    client = RemoteWorkerClient(cfg.base_url, cfg.token, timeout=min(60, cfg.timeout_seconds))
    try:
        handshake = client.handshake()
        advertised = next((item for item in handshake.capabilities
                           if item.name == capability), None)
        if advertised is None or cfg.model not in advertised.models:
            raise ProviderUnavailableError(f"worker does not advertise {capability}:{cfg.model}")
        logical = hashlib.sha256(json.dumps(
            {"capability": capability, "model": cfg.model, "options": options},
            sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        request = WorkerJobRequest(
            idempotency_key=f"inference:{logical}",
            capability=capability, model=cfg.model, options=options,
        )
        status = client.submit(request)
        deadline = time.monotonic() + cfg.timeout_seconds
        while status.state in {"queued", "running"}:
            if time.monotonic() >= deadline:
                client.cancel(status.job_id)
                raise RetryableProviderError("remote inference timed out")
            time.sleep(min(cfg.poll_interval_seconds, 5))
            status = client.status(status.job_id)
        if status.state != "completed" or status.artifact is None \
                or status.artifact.payload is None:
            message = status.error.message if status.error else f"job ended as {status.state}"
            if status.error and status.error.retryable:
                raise RetryableProviderError(message)
            raise RuntimeError(message)
        payload = status.artifact.payload
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(encoded).hexdigest() != status.artifact.sha256:
            raise RuntimeError("remote inference checksum mismatch")
        return payload
    finally:
        client.close()


def run_remote_audio_json(cfg, input_cfg, audio_path: Path, capability: str,
                          options: dict) -> dict:
    if not input_cfg.base_url or not input_cfg.signing_secret:
        raise RuntimeError("remote_input base_url and signing_secret are required")
    sha256 = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    expires = int(time.time()) + input_cfg.url_ttl_seconds
    file_id = audio_path.parent.name
    signature = sign_input(file_id, expires, sha256, input_cfg.signing_secret)
    location = (f"{input_cfg.base_url.rstrip('/')}/api/worker-input/{quote(file_id)}"
                f"?expires={expires}&sha256={sha256}&signature={signature}")
    client = RemoteWorkerClient(cfg.base_url, cfg.token, timeout=min(60, cfg.timeout_seconds))
    try:
        request = WorkerJobRequest(
            idempotency_key=f"{file_id}:{capability}:{cfg.model}:{sha256}",
            capability=capability, model=cfg.model,
            input=JobInput(kind="signed_url", location=location, sha256=sha256,
                           size_bytes=audio_path.stat().st_size, expires_at=expires),
            options=options,
        )
        handshake = client.handshake()
        advertised = next((item for item in handshake.capabilities
                           if item.name == capability), None)
        if advertised is None or cfg.model not in advertised.models:
            raise ProviderUnavailableError(f"worker does not advertise {capability}:{cfg.model}")
        status = client.submit(request)
        deadline = time.monotonic() + cfg.timeout_seconds
        while status.state in {"queued", "running"}:
            if time.monotonic() >= deadline:
                client.cancel(status.job_id)
                raise RetryableProviderError("remote audio inference timed out")
            time.sleep(min(cfg.poll_interval_seconds, 5))
            status = client.status(status.job_id)
        if status.state != "completed" or status.artifact is None \
                or status.artifact.payload is None:
            message = status.error.message if status.error else f"job ended as {status.state}"
            if status.error and status.error.retryable:
                raise RetryableProviderError(message)
            raise RuntimeError(message)
        payload = status.artifact.payload
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(encoded).hexdigest() != status.artifact.sha256:
            raise RuntimeError("remote audio inference checksum mismatch")
        return payload
    finally:
        client.close()
