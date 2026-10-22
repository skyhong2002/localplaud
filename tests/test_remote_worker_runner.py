import json

import httpx
import pytest

from localplaud.providers.remote_worker import JobInput, WorkerJobRequest
from localplaud.providers.remote_worker_runner import (
    RemoteWorkerRunner,
    WorkerHandlerError,
    _validate_connected_peer,
    _validate_fetch_url,
)
from localplaud.providers.remote_worker_server import RemoteWorkerJobStore


def _job(key="one"):
    return WorkerJobRequest(idempotency_key=key, capability="asr", model="turbo",
        input=JobInput(kind="signed_url", location="https://audio.example/signed",
                       sha256="a" * 64, size_bytes=1))


def test_runner_atomically_claims_and_completes_canonical_artifact(tmp_path):
    store = RemoteWorkerJobStore(tmp_path / "jobs.db")
    job = _job()
    store.submit(job)
    runner = RemoteWorkerRunner(store, {"asr": lambda request, progress, cancelled: {
        "segments": [{"text": "hello", "start": 0, "end": 1}],
    }})
    assert runner.run_once() is True
    status = store.get(job.job_id)
    assert status.state == "completed" and status.artifact.payload["segments"]
    encoded = json.dumps(status.artifact.payload, sort_keys=True, separators=(",", ":")).encode()
    import hashlib

    assert hashlib.sha256(encoded).hexdigest() == status.artifact.sha256
    assert runner.run_once() is False


def test_runner_persists_structured_failure_and_claim_is_single_consumer(tmp_path):
    store = RemoteWorkerJobStore(tmp_path / "jobs.db")
    job = _job("failure")
    store.submit(job)

    def fail(*args):
        raise RuntimeError("GPU out of memory")

    runner = RemoteWorkerRunner(store, {"asr": fail})
    assert runner.run_once() is True
    status = store.get(job.job_id)
    assert status.state == "failed"
    assert status.error.code == "execution_failed"
    assert "GPU out of memory" in status.error.message


def test_runner_preserves_handler_retry_classification(tmp_path):
    store = RemoteWorkerJobStore(tmp_path / "jobs.db")
    job = _job("retryable")
    store.submit(job)

    def retryable(*args):
        raise WorkerHandlerError("temporary GPU eviction", code="worker_evicted",
                                 retryable=True, detail={"retry_after": 30})

    assert RemoteWorkerRunner(store, {"asr": retryable}).run_once()
    status = store.get(job.job_id)
    assert status.error.code == "worker_evicted"
    assert status.error.retryable is True
    assert status.error.detail == {"retry_after": 30}
    assert store.submit(job).state == "queued"


class _Stream:
    def __init__(self, peer):
        self.peer = peer

    def get_extra_info(self, name):
        return self.peer if name == "server_addr" else None


def test_fetch_destination_requires_strict_https_host_and_port(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [
        (2, 1, 6, "", ("93.184.216.34", 443)),
    ])
    _validate_fetch_url("https://audio.example/file", {"AUDIO.EXAMPLE."})
    with pytest.raises(ValueError, match="port 443"):
        _validate_fetch_url("https://audio.example:8443/file", {"audio.example"})


@pytest.mark.parametrize("peer", [("127.0.0.1", 443), ("10.0.0.2", 443), None])
def test_connected_peer_validation_fails_closed_for_private_or_missing_peer(peer):
    response = httpx.Response(200, extensions={"network_stream": _Stream(peer)})
    with pytest.raises(ValueError, match="peer|non-global"):
        _validate_connected_peer(response)


def test_connected_peer_validation_accepts_public_peer():
    response = httpx.Response(
        200, extensions={"network_stream": _Stream(("93.184.216.34", 443))}
    )
    _validate_connected_peer(response)
