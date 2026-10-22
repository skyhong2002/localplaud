"""ASR dispatched to an authenticated localplaud remote worker."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import quote

from ..providers.failures import RetryableProviderError
from ..providers.remote_worker import JobInput, RemoteWorkerClient, WorkerJobRequest
from .base import AsrError, AsrUnavailable, Segment, Transcript, Word
from .registry import register


def sign_input(file_id: str, expires: int, sha256: str, secret: str) -> str:
    message = f"{file_id}\n{expires}\n{sha256}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class RemoteWorkerAsrProvider:
    name = "remote-worker"

    def __init__(self, cfg):
        self.cfg = cfg.remote_worker

    def available(self) -> bool:
        return bool(self.cfg.base_url and self.cfg.token and self.cfg.input_base_url
                    and self.cfg.input_signing_secret)

    def transcribe(self, audio_path: Path, language: str = "auto") -> Transcript:
        if not self.available():
            raise AsrError(
                "remote worker requires base_url, token, input_base_url, and input_signing_secret"
            )
        file_id = audio_path.parent.name
        payload_sha = hashlib.sha256(audio_path.read_bytes()).hexdigest()
        expires = int(time.time()) + self.cfg.input_url_ttl_seconds
        signature = sign_input(file_id, expires, payload_sha, self.cfg.input_signing_secret)
        location = (f"{self.cfg.input_base_url.rstrip('/')}/api/worker-input/{quote(file_id)}"
                    f"?expires={expires}&sha256={payload_sha}&signature={signature}")
        client = RemoteWorkerClient(self.cfg.base_url, self.cfg.token,
                                    timeout=min(60, self.cfg.timeout_seconds))
        try:
            handshake = client.handshake()
            capability = next((item for item in handshake.capabilities if item.name == "asr"), None)
            if capability is None or self.cfg.model not in capability.models:
                raise AsrUnavailable(f"worker does not advertise ASR model {self.cfg.model!r}")
            request = WorkerJobRequest(
                idempotency_key=f"{file_id}:asr:{self.cfg.model}:{payload_sha}",
                capability="asr", model=self.cfg.model,
                input=JobInput(kind="signed_url", location=location, sha256=payload_sha,
                               size_bytes=audio_path.stat().st_size, expires_at=expires),
                options={"language": language},
            )
            status = client.submit(request)
            deadline = time.monotonic() + self.cfg.timeout_seconds
            while status.state in {"queued", "running"}:
                if time.monotonic() >= deadline:
                    client.cancel(status.job_id)
                    raise RetryableProviderError("remote worker ASR timed out")
                time.sleep(min(self.cfg.poll_interval_seconds, 5))
                status = client.status(status.job_id)
            if status.state != "completed" or status.artifact is None:
                message = status.error.message if status.error else f"job ended as {status.state}"
                if status.error and status.error.retryable:
                    raise RetryableProviderError(message)
                raise AsrError(f"remote worker ASR failed: {message}")
            data = status.artifact.payload
            if data is None:
                raise AsrError("remote worker returned no inline transcript artifact")
            canonical = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
            if hashlib.sha256(canonical).hexdigest() != status.artifact.sha256:
                raise AsrError("remote worker transcript checksum mismatch")
            return _parse_transcript(data, self.cfg.model)
        finally:
            client.close()


def _parse_transcript(data: dict, model: str) -> Transcript:
    segments = []
    for row in data.get("segments", []):
        words = [Word(text=item.get("text", ""), start=float(item["start"]),
                      end=float(item["end"]), speaker=item.get("speaker"),
                      confidence=item.get("confidence")) for item in row.get("words", [])]
        segments.append(Segment(text=row.get("text", ""), start=float(row["start"]),
                                end=float(row["end"]), speaker=row.get("speaker"), words=words))
    if not segments:
        raise AsrError("remote worker returned an empty transcript")
    return Transcript(segments=segments, language=data.get("language"),
                      duration=data.get("duration"), provider="remote-worker", model=model,
                      has_speakers=bool(data.get("has_speakers")))


@register("remote-worker")
def _factory(cfg):
    return RemoteWorkerAsrProvider(cfg)
