"""Remote worker v1 auth, durability, idempotency, client, and pipeline dispatch."""

from __future__ import annotations

import hashlib

import httpx
import pytest
from fastapi.testclient import TestClient

from localplaud.remote.client import ArtifactChecksumError, RemoteWorkerClient
from localplaud.remote.protocol import (
    ArtifactDescriptor,
    JobResponse,
    JobStage,
    JobStatus,
    JobSubmitRequest,
)


def _client(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.api.app import app
    from localplaud.config import get_settings
    from localplaud.db.session import init_db

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'worker.db'}")
    monkeypatch.setenv("LOCALPLAUD_WORKER_TOKEN", "worker-secret")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()
    return TestClient(app)


def _request(key="same-job"):
    return {
        "protocol_version": "1",
        "idempotency_key": key,
        "stage": "summarize",
        "model": "test",
        "inputs": [
            {
                "name": "transcript",
                "media_type": "application/json",
                "kind": "inline_json",
                "value": {"segments": []},
            }
        ],
        "options": {},
    }


def test_worker_auth_handshake_idempotency_and_persistence(monkeypatch, tmp_path):
    import localplaud.remote.server as server
    from localplaud.db.models import RemoteJob
    from localplaud.db.session import session_scope

    client = _client(monkeypatch, tmp_path)
    assert client.get("/api/worker/v1/capabilities").status_code == 401
    headers = {"authorization": "Bearer worker-secret"}
    handshake = client.get("/api/worker/v1/capabilities", headers=headers)
    assert handshake.status_code == 200
    assert handshake.json()["protocol"] == "localplaud-worker"
    assert handshake.json()["version"] == "1"

    monkeypatch.setattr(
        server,
        "_execute",
        lambda request: [server._artifact("result.json", "application/json", b'{"ok":true}')],
    )
    first = client.post("/api/worker/v1/jobs", headers=headers, json=_request())
    assert first.status_code == 200
    job_id = first.json()["job_id"]
    status = client.get(f"/api/worker/v1/jobs/{job_id}", headers=headers).json()
    assert status["status"] == "succeeded"
    assert status["progress"] == 1.0
    second = client.post("/api/worker/v1/jobs", headers=headers, json=_request())
    assert second.json()["job_id"] == job_id
    artifact = client.get(status["artifacts"][0]["download_url"], headers=headers)
    assert artifact.content == b'{"ok":true}'
    with session_scope() as session:
        assert session.query(RemoteJob).count() == 1
        assert session.get(RemoteJob, job_id).artifacts


def test_worker_cancel_and_structured_retry_error(monkeypatch, tmp_path):
    import localplaud.remote.server as server

    client = _client(monkeypatch, tmp_path)
    headers = {"authorization": "Bearer worker-secret"}
    real_execute_job = server.execute_job
    monkeypatch.setattr(server, "execute_job", lambda job_id: None)
    queued = client.post("/api/worker/v1/jobs", headers=headers, json=_request("cancel-me")).json()
    cancelled = client.post(
        f"/api/worker/v1/jobs/{queued['job_id']}/cancel", headers=headers
    ).json()
    assert cancelled["status"] == "cancelled"

    monkeypatch.setattr(server, "_execute", lambda request: (_ for _ in ()).throw(OSError("GPU busy")))
    failed = client.post("/api/worker/v1/jobs", headers=headers, json=_request("retry-me")).json()
    # Restore the real runner because submit captured the monkeypatched no-op above.
    real_execute_job(failed["job_id"])
    result = client.get(f"/api/worker/v1/jobs/{failed['job_id']}", headers=headers).json()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "stage_execution_failed"
    assert result["error"]["retryable"] is True


def test_restart_recovery_resumes_durable_queued_job(monkeypatch, tmp_path):
    import localplaud.remote.server as server
    from localplaud.db.models import RemoteJob
    from localplaud.db.session import session_scope

    _client(monkeypatch, tmp_path)
    request = JobSubmitRequest.model_validate(_request("restart-job"))
    with session_scope() as session:
        session.add(
            RemoteJob(
                id="restart",
                idempotency_key=request.idempotency_key,
                stage=request.stage,
                model=request.model,
                input_manifest=request.model_dump(mode="json"),
                status="queued",
            )
        )
    monkeypatch.setattr(
        server,
        "_execute",
        lambda request: [server._artifact("result.json", "application/json", b"{}")],
    )
    server.resume_pending_jobs()
    with session_scope() as session:
        assert session.get(RemoteJob, "restart").status == "succeeded"


def test_protocol_rejects_credentials_recursively():
    payload = _request("unsafe")
    payload["options"] = {"nested": {"plaud_token": "do-not-send"}}
    with pytest.raises(ValueError, match="credentials"):
        JobSubmitRequest.model_validate(payload)


def test_client_verifies_artifact_checksum():
    expected = hashlib.sha256(b"expected").hexdigest()
    job = JobResponse(
        job_id="job",
        idempotency_key="key",
        stage=JobStage.summarize,
        status=JobStatus.succeeded,
        progress=1,
        artifacts=[
            ArtifactDescriptor(
                name="result.json",
                media_type="application/json",
                size=8,
                sha256=expected,
                download_url="/artifact",
            )
        ],
    )

    def handler(request):
        return httpx.Response(200, content=b"corrupt")

    client = RemoteWorkerClient(
        "http://worker/", "token", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ArtifactChecksumError):
        client.wait(job)
    client.close()


def test_worker_registry_creates_remote_connection_and_persists_handshake(monkeypatch, tmp_path):
    from localplaud.db.models import ModelCatalogEntry, ProviderConnection, RemoteWorker
    from localplaud.db.session import session_scope
    from localplaud.remote.protocol import HandshakeResponse, StageCapability

    client = _client(monkeypatch, tmp_path)
    headers = {"authorization": "Bearer worker-secret"}

    class FakeClient:
        def handshake(self):
            return HandshakeResponse(
                worker_id="gpu-1",
                capabilities=[StageCapability(stage="transcribe", models=["turbo"])],
            )

        def close(self):
            pass

    monkeypatch.setattr(
        "localplaud.remote.registry.RemoteWorkerClient.from_config",
        lambda config: FakeClient(),
    )
    created = client.post(
        "/api/providers/workers",
        headers=headers,
        json={
            "key": "gpu-1",
            "name": "GPU 1",
            "base_url": "https://worker.example/",
            "token_env": "LOCALPLAUD_WORKER_TOKEN",
        },
    )
    assert created.status_code == 201
    worker_id = created.json()["id"]
    health = client.post(f"/api/providers/workers/{worker_id}/health", headers=headers)
    assert health.json()["status"] == "healthy"
    with session_scope() as session:
        worker = session.get(RemoteWorker, worker_id)
        assert worker.protocol_version == "1"
        connection = session.query(ProviderConnection).filter_by(key="worker:gpu-1").one()
        assert connection.execution_target == "remote_worker"
        assert connection.secret_ref == "env:LOCALPLAUD_WORKER_TOKEN"
        assert "worker-secret" not in str(connection.config)
        model = session.query(ModelCatalogEntry).filter_by(connection_id=connection.id).one()
        assert model.model_key == "turbo"
        assert model.capabilities["execution_target"] == "remote_worker"
    assert client.delete(f"/api/providers/workers/{worker_id}", headers=headers).status_code == 204


def test_pipeline_uses_remote_transcribe_selection(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    import localplaud.worker.pipeline as pipeline
    from localplaud.config import Settings, get_settings
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'pipeline.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFFremote")
    with session_scope() as session:
        session.add(
            PlaudFile(id="remote", status=FileStatus.downloaded, audio_path=str(audio))
        )

    snapshot = {
        "policy": {"no_egress": False, "fallback_policy": {}},
        "stages": {
            "transcribe": {
                "connection": "asr:worker",
                "model": "turbo",
                "options": {},
                "execution_target": "remote_worker",
                "data_egress": True,
            }
        },
        "layers": ["test"],
    }

    class Resolved:
        def to_dict(self):
            return snapshot

    monkeypatch.setattr(
        pipeline,
        "resolve_recording_profile",
        lambda session, file_id, **_kwargs: Resolved(),
    )
    monkeypatch.setattr(
        pipeline,
        "_run_remote_stage",
        lambda *args, **kwargs: {
            "segments": [{"text": "remote transcript", "start": 0, "end": 1}],
            "language": "en",
            "model": "turbo",
        },
    )
    settings = Settings(
        pipeline={
            "convert": False,
            "diarize": False,
            "summarize": False,
            "mind_map": False,
            "index": False,
        }
    )
    pipeline.process_file("remote", settings)
    with session_scope() as session:
        file = session.get(PlaudFile, "remote")
        transcript = file.local_transcript
        assert transcript.text == "remote transcript"
        assert transcript.provider == "remote-worker"
        assert next(run for run in file.stage_runs if run.stage == "transcribe").provider == "remote-worker"
