import json

from localplaud.providers.remote_worker import JobInput, WorkerJobRequest
from localplaud.providers.remote_worker_runner import RemoteWorkerRunner, WorkerHandlerError
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
