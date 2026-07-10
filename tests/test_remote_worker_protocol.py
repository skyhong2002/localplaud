import json
import time

import httpx

from localplaud.providers.remote_worker import (
    PROTOCOL_VERSION,
    JobInput,
    RemoteWorkerClient,
    WorkerJobRequest,
    artifact_checksum,
    signed_headers,
    verify_signature,
)


def test_signature_detects_tampering_and_expiry(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1_000)
    body = b'{"job":"one"}'
    headers = signed_headers("token", "POST", "/v1/jobs", body)
    assert verify_signature("token", "POST", "/v1/jobs", body,
                            headers["X-Localplaud-Timestamp"],
                            headers["X-Localplaud-Signature"])
    assert not verify_signature("token", "POST", "/v1/jobs", b"tampered",
                                headers["X-Localplaud-Timestamp"],
                                headers["X-Localplaud-Signature"])
    monkeypatch.setattr(time, "time", lambda: 2_000)
    assert not verify_signature("token", "POST", "/v1/jobs", body,
                                headers["X-Localplaud-Timestamp"],
                                headers["X-Localplaud-Signature"])


def test_client_handshake_submit_status_and_cancel():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.method, request.url.path))
        assert request.headers["x-localplaud-protocol"] == PROTOCOL_VERSION
        if request.url.path == "/v1/handshake":
            return httpx.Response(200, json={"protocol_version": "1", "worker_id": "gpu-1",
                "capabilities": [{"name": "asr", "models": ["large-v3-turbo"]}]})
        if request.url.path == "/v1/jobs" and request.method == "POST":
            data = json.loads(request.content)
            return httpx.Response(200, json={"job_id": data["job_id"],
                "idempotency_key": data["idempotency_key"], "state": "queued", "progress": 0})
        job_id = request.url.path.split("/")[3]
        state = "cancelled" if request.url.path.endswith("/cancel") else "running"
        return httpx.Response(200, json={"job_id": job_id, "idempotency_key": "recording:asr:v1",
                                        "state": state, "progress": 0.5})

    client = RemoteWorkerClient("https://worker.example", "token",
                                transport=httpx.MockTransport(handler))
    assert client.handshake().worker_id == "gpu-1"
    request = WorkerJobRequest(idempotency_key="recording:asr:v1", capability="asr",
        model="large-v3-turbo", input=JobInput(kind="signed_url",
        location="https://audio.example/signed", sha256="a" * 64, size_bytes=42))
    queued = client.submit(request)
    assert queued.state == "queued"
    assert client.status(request.job_id).state == "running"
    assert client.cancel(request.job_id).state == "cancelled"
    assert seen == [("GET", "/v1/handshake"), ("POST", "/v1/jobs"),
                    ("GET", f"/v1/jobs/{request.job_id}"),
                    ("POST", f"/v1/jobs/{request.job_id}/cancel")]
    assert artifact_checksum(b"result") == "f6a214f7a5fcda0c2cee9660b7fc29f5649e3c68aad48e20e950137c98913a68"
