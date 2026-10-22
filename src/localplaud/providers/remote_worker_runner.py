"""Durable remote-worker job consumer and built-in ASR handler."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..asr.base import Transcript
from ..asr.registry import build_provider
from ..config import AsrConfig, EmbeddingsConfig, LlmConfig
from ..embeddings.base import build_embedder
from ..llm.base import build_llm
from .remote_worker import StructuredWorkerError, WorkerJobRequest
from .remote_worker_server import RemoteWorkerJobStore

Progress = Callable[[float], None]
Cancelled = Callable[[], bool]
JobHandler = Callable[[WorkerJobRequest, Progress, Cancelled], dict[str, Any]]


class WorkerHandlerError(RuntimeError):
    """A handler failure with protocol-visible retry classification."""

    def __init__(self, message: str, *, code: str = "execution_failed",
                 retryable: bool = False, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.detail = detail or {}


class RemoteWorkerRunner:
    def __init__(self, store: RemoteWorkerJobStore, handlers: dict[str, JobHandler],
                 *, lease_seconds: float = 120):
        self.store = store
        self.handlers = handlers
        self.lease_seconds = lease_seconds

    def run_once(self) -> bool:
        request = self.store.claim_next(set(self.handlers), lease_seconds=self.lease_seconds)
        if request is None:
            return False
        handler = self.handlers[request.capability]
        stop_heartbeat = threading.Event()

        def heartbeat() -> None:
            while not stop_heartbeat.wait(max(1, self.lease_seconds / 3)):
                try:
                    self.store.renew_claim(
                        request.job_id, lease_seconds=self.lease_seconds
                    )
                except Exception:  # claim loss is enforced again on completion
                    return

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        try:
            if self.store.cancellation_requested(request.job_id):
                self.store.cancel(request.job_id)
                return True
            payload = handler(
                request,
                lambda value: self.store.progress(request.job_id, value),
                lambda: self.store.cancellation_requested(request.job_id),
            )
            if self.store.cancellation_requested(request.job_id):
                self.store.cancel(request.job_id)
                return True
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            self.store.complete(request.job_id, encoded, content_type="application/json",
                                inline_payload=payload)
        except WorkerHandlerError as exc:
            if self.store.cancellation_requested(request.job_id):
                return True
            self.store.fail(request.job_id, StructuredWorkerError(
                code=exc.code, message=str(exc)[:1000], retryable=exc.retryable,
                detail=exc.detail,
            ))
        except Exception as exc:  # noqa: BLE001 - durable structured failure boundary
            if self.store.cancellation_requested(request.job_id):
                return True
            self.store.fail(request.job_id, StructuredWorkerError(
                code="execution_failed", message=str(exc)[:1000], retryable=False,
                detail={"exception_type": type(exc).__name__},
            ))
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=1)
        return True


def build_asr_handler(config: AsrConfig, *, allowed_fetch_hosts: set[str],
                      max_input_bytes: int = 2_000_000_000) -> JobHandler:
    if config.provider == "remote-worker":
        raise ValueError("remote worker ASR handler cannot recursively use remote-worker")
    if not allowed_fetch_hosts:
        raise ValueError("at least one worker input host must be explicitly allowed")

    def handler(request: WorkerJobRequest, progress: Progress, cancelled: Cancelled) -> dict:
        if request.input.kind != "signed_url":
            raise ValueError("built-in ASR handler requires signed_url input")
        _validate_fetch_url(request.input.location, allowed_fetch_hosts)
        if request.input.size_bytes > max_input_bytes:
            raise ValueError("worker input exceeds configured byte limit")
        progress(0.05)
        suffix = Path(urlparse(request.input.location).path).suffix or ".audio"
        with tempfile.TemporaryDirectory(prefix="localplaud-worker-") as directory:
            path = Path(directory) / f"input{suffix}"
            digest = hashlib.sha256()
            size = 0
            with httpx.stream("GET", request.input.location, timeout=120,
                              follow_redirects=False, trust_env=False) as response:
                response.raise_for_status()
                _validate_connected_peer(response)
                with path.open("wb") as output:
                    for chunk in response.iter_bytes():
                        if cancelled():
                            raise RuntimeError("job cancelled")
                        size += len(chunk)
                        if size > max_input_bytes or size > request.input.size_bytes:
                            raise ValueError("worker input exceeded declared size")
                        digest.update(chunk)
                        output.write(chunk)
            if size != request.input.size_bytes or digest.hexdigest() != request.input.sha256:
                raise ValueError("worker input size or checksum mismatch")
            progress(0.2)
            provider = build_provider(config.provider, config)
            transcript = provider.transcribe(path, language=request.options.get("language", "auto"))
            progress(0.95)
            return _transcript_payload(transcript)

    return handler


def build_text_handler(config: LlmConfig) -> JobHandler:
    if config.provider == "remote-worker":
        raise ValueError("remote text handler cannot recursively use remote-worker")

    def handler(request: WorkerJobRequest, progress: Progress, cancelled: Cancelled) -> dict:
        if request.input is not None:
            raise ValueError("text jobs must not include audio input")
        if cancelled():
            raise RuntimeError("job cancelled")
        progress(0.1)
        llm = build_llm(config)
        text = llm.complete(
            request.options.get("prompt", ""), system=request.options.get("system"),
            temperature=float(request.options.get("temperature", 0.3)),
            max_tokens=int(request.options.get("max_tokens", 2048)),
        )
        progress(0.95)
        return {"text": text, "usage": getattr(llm, "usage", {})}

    return handler


def build_embeddings_handler(config: EmbeddingsConfig) -> JobHandler:
    if config.provider == "remote-worker":
        raise ValueError("remote embedding handler cannot recursively use remote-worker")

    def handler(request: WorkerJobRequest, progress: Progress, cancelled: Cancelled) -> dict:
        if request.input is not None:
            raise ValueError("embedding jobs must not include audio input")
        texts = request.options.get("texts")
        if not isinstance(texts, list) or not all(isinstance(item, str) for item in texts):
            raise ValueError("embedding job requires a string list")
        if cancelled():
            raise RuntimeError("job cancelled")
        progress(0.1)
        vectors = build_embedder(config).embed(texts)
        progress(0.95)
        return {"vectors": vectors}

    return handler


def build_correction_handler(config: LlmConfig) -> JobHandler:
    if config.provider == "remote-worker":
        raise ValueError("remote correction handler cannot recursively use remote-worker")

    def handler(request: WorkerJobRequest, progress: Progress, cancelled: Cancelled) -> dict:
        transcript = request.options.get("transcript")
        if request.input is not None or not isinstance(transcript, dict):
            raise ValueError("correction job requires one transcript object and no audio")
        if cancelled():
            raise RuntimeError("job cancelled")
        prompt = (
            "Correct names, terminology, punctuation, and obvious recognition errors in this "
            "transcript JSON. Preserve segment/word count, all timestamps, speakers, and every "
            "non-text field exactly. Return only the complete corrected JSON object.\n\n"
            + json.dumps(transcript, ensure_ascii=False, separators=(",", ":"))
        )
        progress(0.1)
        raw = build_llm(config).complete(
            prompt, system="Never add facts or alter timing/speaker metadata.",
            temperature=0, max_tokens=8000,
        ).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("correction model returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ValueError("correction model returned a non-object")
        progress(0.95)
        return result

    return handler


def _validate_fetch_url(value: str, allowed_hosts: set[str]) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("worker fetch URL must be credential-free HTTPS")
    if parsed.port not in (None, 443) or parsed.fragment:
        raise ValueError("worker fetch URL must use HTTPS port 443 without a fragment")
    hostname = parsed.hostname.rstrip(".").lower()
    normalized_hosts = {host.rstrip(".").lower() for host in allowed_hosts}
    if hostname not in normalized_hosts:
        raise ValueError("worker fetch host is not allowlisted")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, 443,
                                                               type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError("worker fetch host cannot be resolved") from exc
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("worker fetch URL resolves to a non-global address")


def _validate_connected_peer(response: httpx.Response) -> None:
    """Reject a rebinding/private peer before consuming any response body.

    httpx does not expose a supported way to pin a resolved address while retaining
    ordinary TLS hostname/SNI verification.  The network stream does expose the
    connected peer on the standard transport; fail closed if it is absent.  Operators
    must still restrict outbound traffic so this worker can reach only the explicitly
    allowlisted public object-store hosts on TCP/443.
    """
    stream = response.extensions.get("network_stream")
    peer = stream.get_extra_info("server_addr") if stream is not None else None
    if not peer or not isinstance(peer, tuple) or not peer[0]:
        raise ValueError("worker fetch connected peer could not be verified")
    try:
        address = ipaddress.ip_address(peer[0])
    except ValueError as exc:
        raise ValueError("worker fetch connected peer is not an IP address") from exc
    if not address.is_global:
        raise ValueError("worker fetch connected to a non-global address")


def _transcript_payload(transcript: Transcript) -> dict[str, Any]:
    return {
        "language": transcript.language, "duration": transcript.duration,
        "has_speakers": transcript.has_speakers,
        "segments": [{"text": segment.text, "start": segment.start, "end": segment.end,
                      "speaker": segment.speaker,
                      "words": [{"text": word.text, "start": word.start, "end": word.end,
                                 "speaker": word.speaker, "confidence": word.confidence}
                                for word in segment.words]}
                     for segment in transcript.segments],
    }
