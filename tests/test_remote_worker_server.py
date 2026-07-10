import json
import time

from fastapi.testclient import TestClient

from localplaud.providers.remote_worker import (
    JobInput,
    StructuredWorkerError,
    WorkerCapability,
    WorkerJobRequest,
    artifact_checksum,
    signed_headers,
)
from localplaud.providers.remote_worker_server import (
    InvalidTransitionError,
    RemoteWorkerJobStore,
    create_remote_worker_app,
)

TOKEN = "worker-test-token"


def _request(client: TestClient, method: str, path: str, payload=None):
    body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
    return client.request(method, path, content=body, headers={
        "Content-Type": "application/json",
        **signed_headers(TOKEN, method, path, body),
    })


def _job(key="recording-1:asr:revision-1", **options):
    return WorkerJobRequest(
        idempotency_key=key,
        capability="asr",
        model="large-v3-turbo",
        input=JobInput(kind="signed_url", location="https://files.example/short-lived",
                       sha256="a" * 64, size_bytes=1024, expires_at=2_000_000_000),
        options=options,
    )


def _client(tmp_path):
    store = RemoteWorkerJobStore(tmp_path / "jobs.sqlite3")
    app = create_remote_worker_app(
        token=TOKEN,
        worker_id="test-gpu",
        capabilities=[WorkerCapability(name="asr", models=["large-v3-turbo"])],
        store=store,
        runtime={"device": "cuda"},
    )
    return TestClient(app), store


def test_server_authenticates_handshake_and_returns_structured_auth_errors(tmp_path):
    client, _ = _client(tmp_path)
    response = _request(client, "GET", "/v1/handshake")
    assert response.status_code == 200
    assert response.json()["worker_id"] == "test-gpu"
    assert response.json()["runtime"] == {
        "device": "cuda", "queue": {"queued": 0, "running": 0}
    }

    unsigned = client.get("/v1/handshake", headers={"X-Localplaud-Protocol": "1"})
    assert unsigned.status_code == 401
    assert unsigned.json()["error"]["code"] == "authentication_failed"

    wrong_version = client.get("/v1/handshake", headers={"X-Localplaud-Protocol": "99"})
    assert wrong_version.status_code == 426
    assert wrong_version.json()["error"]["code"] == "protocol_mismatch"


def test_submit_is_idempotent_across_server_restart_and_conflicts_are_rejected(tmp_path):
    client, _ = _client(tmp_path)
    original = _job()
    first = _request(client, "POST", "/v1/jobs", original.model_dump(mode="json"))
    assert first.status_code == 200
    assert first.json()["state"] == "queued"

    # A network retry commonly has a new attempt/job id; the durable result wins.
    retry = _job()
    second = _request(client, "POST", "/v1/jobs", retry.model_dump(mode="json"))
    assert second.status_code == 200
    assert second.json()["job_id"] == original.job_id

    restarted, _ = _client(tmp_path)
    third = _request(restarted, "POST", "/v1/jobs", retry.model_dump(mode="json"))
    assert third.json()["job_id"] == original.job_id

    changed = _job(language="zh")
    conflict = _request(restarted, "POST", "/v1/jobs", changed.model_dump(mode="json"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_progress_completion_checksum_status_and_terminal_result_are_durable(tmp_path):
    client, store = _client(tmp_path)
    job = _job()
    _request(client, "POST", "/v1/jobs", job.model_dump(mode="json"))
    store.start(job.job_id)
    store.progress(job.job_id, 0.55)

    running = _request(client, "GET", f"/v1/jobs/{job.job_id}")
    assert running.json()["state"] == "running"
    assert running.json()["progress"] == 0.55

    completed = store.complete(job.job_id, b'{"segments":[]}',
                               content_type="application/json", inline_payload={"segments": []})
    assert completed.artifact is not None
    assert completed.artifact.sha256 == artifact_checksum(b'{"segments":[]}')
    assert store.complete(job.job_id, b"different", content_type="text/plain") == completed

    restarted = RemoteWorkerJobStore(tmp_path / "jobs.sqlite3")
    recovered = restarted.get(job.job_id)
    assert recovered == completed
    assert restarted.request_payload(job.job_id).input.location == "https://files.example/short-lived"


def test_cancel_is_idempotent_and_prevents_late_completion(tmp_path):
    client, store = _client(tmp_path)
    job = _job()
    _request(client, "POST", "/v1/jobs", job.model_dump(mode="json"))
    cancelled = _request(client, "POST", f"/v1/jobs/{job.job_id}/cancel", {})
    assert cancelled.json()["state"] == "cancelled"
    assert store.cancellation_requested(job.job_id)
    assert _request(client, "POST", f"/v1/jobs/{job.job_id}/cancel", {}).json() == cancelled.json()

    try:
        store.complete(job.job_id, b"late", content_type="text/plain")
    except InvalidTransitionError:
        pass
    else:
        raise AssertionError("cancelled work accepted a late artifact")


def test_expired_claim_is_recovered_and_stale_owner_cannot_complete(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    first = RemoteWorkerJobStore(path)
    job = _job("lease-recovery")
    first.submit(job)
    assert first.claim_next({"asr"}, lease_seconds=0.01).job_id == job.job_id
    time.sleep(0.02)

    replacement = RemoteWorkerJobStore(path)
    assert replacement.claim_next({"asr"}, lease_seconds=60).job_id == job.job_id
    try:
        first.complete(job.job_id, b"stale", content_type="text/plain")
    except InvalidTransitionError:
        pass
    else:
        raise AssertionError("an expired worker claim overwrote the replacement owner")

    completed = replacement.complete(job.job_id, b"fresh", content_type="text/plain")
    assert completed.state == "completed"


def test_failures_and_validation_use_structured_errors_and_credentials_are_rejected(tmp_path):
    client, store = _client(tmp_path)
    job = _job()
    _request(client, "POST", "/v1/jobs", job.model_dump(mode="json"))
    failed = store.fail(job.job_id, StructuredWorkerError(
        code="gpu_oom", message="GPU memory exhausted", retryable=True,
        detail={"required_mib": 9000},
    ))
    assert failed.error is not None and failed.error.retryable
    retried = store.submit(job)
    assert retried.state == "queued"
    assert retried.error is None

    unsupported = _job(key="other")
    unsupported.model = "unknown"
    response = _request(client, "POST", "/v1/jobs", unsupported.model_dump(mode="json"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_model"

    leaked = _job(key="leaked", plaud_access_token="secret")
    response = _request(client, "POST", "/v1/jobs", leaked.model_dump(mode="json"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "forbidden_credentials"

    top_level_leak = _job(key="top-level").model_dump(mode="json")
    top_level_leak["plaud_refresh_token"] = "secret"
    response = _request(client, "POST", "/v1/jobs", top_level_leak)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "forbidden_credentials"

    generic_leak = _job(key="generic-secret", api_key="secret")
    response = _request(client, "POST", "/v1/jobs", generic_leak.model_dump(mode="json"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "forbidden_credentials"

    # Normal generation limits are not credentials.
    safe_tokens = _job(key="safe-max-tokens", max_tokens=800)
    assert _request(client, "POST", "/v1/jobs",
                    safe_tokens.model_dump(mode="json")).status_code == 200

    missing = _request(client, "GET", "/v1/jobs/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "job_not_found"
