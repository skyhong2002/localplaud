"""Remote worker v1 auth, durability, idempotency, client, and pipeline dispatch."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from localplaud.remote.client import (
    ArtifactChecksumError,
    RemoteWorkerClient,
    RemoteWorkerError,
)
from localplaud.remote.protocol import (
    ArtifactDescriptor,
    InputReference,
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


def test_remote_diarization_artifact_reports_its_model(monkeypatch):
    import base64

    import localplaud.remote.server as server
    from localplaud.config import Settings

    monkeypatch.setattr(server, "get_settings", lambda: Settings())
    monkeypatch.setattr(
        "localplaud.worker.diarize.diarize",
        lambda _path, transcript, _config: transcript,
    )
    transcript = {"segments": [], "has_speakers": False}
    request = JobSubmitRequest(
        idempotency_key="diarize-model",
        stage=JobStage.diarize,
        model="pyannote-requested",
        inputs=[
            InputReference(
                name="audio",
                media_type="audio/wav",
                kind="inline_base64",
                value=base64.b64encode(b"RIFF").decode(),
            ),
            InputReference(
                name="transcript",
                media_type="application/json",
                kind="inline_json",
                value=transcript,
            ),
        ],
    )

    artifact = server._execute(request)[0]
    payload = json.loads(base64.b64decode(artifact["data_base64"]))

    assert payload["model"] == "pyannote-requested"


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


def test_wait_clamps_poll_and_artifact_requests_to_one_deadline(monkeypatch):
    import localplaud.remote.client as client_module

    now = 100.0

    def monotonic():
        return now

    def sleep(seconds):
        nonlocal now
        now += seconds

    monkeypatch.setattr(client_module.time, "monotonic", monotonic)
    monkeypatch.setattr(client_module.time, "sleep", sleep)
    observed: list[tuple[str, float]] = []
    payload = b"done"
    checksum = hashlib.sha256(payload).hexdigest()

    def handler(request):
        timeouts = request.extensions["timeout"]
        observed.append((request.url.path, max(timeouts.values())))
        if request.url.path.endswith("/artifact"):
            return httpx.Response(200, content=payload)
        return httpx.Response(
            200,
            json={
                "job_id": "job",
                "idempotency_key": "key",
                "stage": "summarize",
                "status": "succeeded",
                "progress": 1,
                "artifacts": [
                    {
                        "name": "result.json",
                        "media_type": "application/json",
                        "size": len(payload),
                        "sha256": checksum,
                        "download_url": "/artifact",
                    }
                ],
            },
        )

    queued = JobResponse(
        job_id="job",
        idempotency_key="key",
        stage=JobStage.summarize,
        status=JobStatus.queued,
        progress=0,
    )
    client = RemoteWorkerClient(
        "http://worker/", "token", timeout=100, transport=httpx.MockTransport(handler)
    )
    result = client.wait(queued, timeout=10, initial_backoff=2)
    assert result.artifacts == {"result.json": payload}
    assert observed == [("/api/worker/v1/jobs/job", 8.0), ("/artifact", 8.0)]
    client.close()


def test_submit_and_wait_includes_submit_in_the_end_to_end_deadline(monkeypatch):
    import localplaud.remote.client as client_module

    now = 50.0

    monkeypatch.setattr(client_module.time, "monotonic", lambda: now)
    payload = b"done"
    checksum = hashlib.sha256(payload).hexdigest()
    observed: list[tuple[str, float]] = []

    def handler(request):
        nonlocal now
        timeouts = request.extensions["timeout"]
        observed.append((request.url.path, max(timeouts.values())))
        if request.method == "POST":
            now += 4
            return httpx.Response(
                200,
                json={
                    "job_id": "job",
                    "idempotency_key": "same-job",
                    "stage": "summarize",
                    "status": "succeeded",
                    "progress": 1,
                    "artifacts": [
                        {
                            "name": "result.json",
                            "media_type": "application/json",
                            "size": len(payload),
                            "sha256": checksum,
                            "download_url": "/artifact",
                        }
                    ],
                },
            )
        return httpx.Response(200, content=payload)

    client = RemoteWorkerClient(
        "http://worker/", "token", timeout=100, transport=httpx.MockTransport(handler)
    )
    result = client.submit_and_wait(
        JobSubmitRequest.model_validate(_request()), timeout=10
    )
    assert result.artifacts == {"result.json": payload}
    assert observed == [("/api/worker/v1/jobs", 10.0), ("/artifact", 6.0)]
    client.close()


def test_submit_and_wait_rejects_a_submit_that_consumes_the_deadline(monkeypatch):
    import localplaud.remote.client as client_module

    now = 10.0
    monkeypatch.setattr(client_module.time, "monotonic", lambda: now)

    def handler(_request):
        nonlocal now
        now += 10
        return httpx.Response(
            200,
            json={
                "job_id": "job",
                "idempotency_key": "same-job",
                "stage": "summarize",
                "status": "succeeded",
                "progress": 1,
                "artifacts": [],
            },
        )

    client = RemoteWorkerClient(
        "http://worker/", "token", timeout=100, transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RemoteWorkerError, match="timed out"):
        client.submit_and_wait(JobSubmitRequest.model_validate(_request()), timeout=10)
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
        connection_id = connection.id
        model_id = model.id

    renamed = client.put(
        f"/api/providers/workers/{worker_id}",
        headers=headers,
        json={
            "key": "gpu-renamed",
            "name": "GPU renamed",
            "base_url": "https://renamed.example/",
            "token_env": "LOCALPLAUD_WORKER_TOKEN",
            "enabled": True,
        },
    )
    assert renamed.status_code == 200
    with session_scope() as session:
        assert session.query(ProviderConnection).filter_by(key="worker:gpu-1").first() is None
        connection = session.query(ProviderConnection).filter_by(
            key="worker:gpu-renamed"
        ).one()
        model = session.get(ModelCatalogEntry, model_id)
        assert connection.id == connection_id
        assert model.connection_id == connection_id

    disabled = client.put(
        f"/api/providers/workers/{worker_id}",
        headers=headers,
        json={
            "key": "gpu-renamed",
            "name": "GPU renamed",
            "base_url": "https://renamed.example/",
            "token_env": "LOCALPLAUD_WORKER_TOKEN",
            "enabled": False,
        },
    )
    assert disabled.status_code == 200
    from localplaud.providers.resolver import ResolutionError, resolve_profile
    from localplaud.providers.service import _capability_catalog, _connection_catalog

    with session_scope() as session:
        with pytest.raises(ResolutionError, match="unknown provider/model"):
            resolve_profile(
                [
                    {
                        "key": "disabled-worker",
                        "stages": {
                            "transcribe": {
                                "connection": "worker:gpu-renamed",
                                "model": "turbo",
                            }
                        },
                    }
                ],
                _capability_catalog(session),
                _connection_catalog(session),
            )
    assert client.delete(f"/api/providers/workers/{worker_id}", headers=headers).status_code == 204


def test_worker_rename_collision_preserves_existing_identity(monkeypatch, tmp_path):
    from localplaud.db.models import ProviderConnection, RemoteWorker
    from localplaud.db.session import session_scope
    from localplaud.remote.registry import save_worker

    client = _client(monkeypatch, tmp_path)
    with session_scope() as session:
        created = save_worker(
            session,
            {
                "key": "source",
                "name": "Source",
                "base_url": "https://source.example/",
                "token_env": "LOCALPLAUD_WORKER_TOKEN",
            },
        )
        worker_id = created["id"]
        original_connection_id = session.query(ProviderConnection).filter_by(
            key="worker:source"
        ).one().id
        session.add(
            ProviderConnection(
                key="worker:taken",
                name="Taken",
                provider_type="localplaud-worker",
            )
        )

    response = client.put(
        f"/api/providers/workers/{worker_id}",
        headers={"authorization": "Bearer worker-secret"},
        json={
            "key": "taken",
            "name": "Changed",
            "base_url": "https://changed.example/",
            "token_env": "LOCALPLAUD_WORKER_TOKEN",
        },
    )

    assert response.status_code == 409
    assert "provider connection key already exists" in response.json()["detail"]
    with session_scope() as session:
        assert session.get(RemoteWorker, worker_id).key == "source"
        assert session.query(ProviderConnection).filter_by(key="worker:source").one().id == (
            original_connection_id
        )


def test_worker_update_is_fenced_by_active_processing(monkeypatch, tmp_path):
    from localplaud.db.models import PlaudFile, ProviderConnection, RemoteWorker
    from localplaud.db.session import session_scope

    client = _client(monkeypatch, tmp_path)
    headers = {"authorization": "Bearer worker-secret"}
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
    worker_id = created.json()["id"]
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="busy",
                processing_token="active-worker",
                processing_lease_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        )

    response = client.put(
        f"/api/providers/workers/{worker_id}",
        headers=headers,
        json={
            "key": "gpu-1",
            "name": "Changed GPU",
            "base_url": "https://changed.example/",
            "token_env": "CHANGED_WORKER_TOKEN",
            "timeout": 15,
        },
    )

    assert response.status_code == 409
    assert "processing" in response.json()["detail"]
    with session_scope() as session:
        worker = session.get(RemoteWorker, worker_id)
        connection = session.query(ProviderConnection).filter_by(key="worker:gpu-1").one()
        assert worker.name == "GPU 1"
        assert worker.base_url == "https://worker.example/"
        assert connection.name == "GPU 1"
        assert connection.config["base_url"] == "https://worker.example/"
        assert connection.secret_ref == "env:LOCALPLAUD_WORKER_TOKEN"


def test_remote_dispatch_uses_resolved_non_secret_configuration(monkeypatch):
    import localplaud.worker.pipeline as pipeline

    observed = {}

    class FakeClient:
        def submit_and_wait(self, request, timeout):
            observed["request"] = request
            observed["timeout"] = timeout
            return SimpleNamespace(
                artifacts={"result.json": json.dumps({"model": "summary-model"}).encode()}
            )

        def close(self):
            observed["closed"] = True

    def from_config(config):
        observed["config"] = config
        return FakeClient()

    monkeypatch.setattr(pipeline.RemoteWorkerClient, "from_config", from_config)
    snapshot = {
        "stages": {
            "summarize": {
                "connection": "worker:deleted-after-resolution",
                "model": "summary-model",
                "execution_target": "remote_worker",
                "configuration": {
                    "base_url": "https://snapshot.example/",
                    "timeout": 9,
                    "job_timeout": 27,
                },
                "secret_ref": "env:SNAPSHOT_WORKER_TOKEN",
            }
        }
    }

    result = pipeline._run_remote_stage(
        "recording", snapshot, "summarize", [pipeline._remote_json_input("transcript", {})]
    )

    assert result["model"] == "summary-model"
    assert observed["config"] == {
        "base_url": "https://snapshot.example/",
        "timeout": 9,
        "job_timeout": 27,
        "token_env": "SNAPSHOT_WORKER_TOKEN",
    }
    assert observed["timeout"] == 27
    assert "worker-secret" not in str(observed["config"])
    assert observed["closed"] is True


@pytest.mark.parametrize("stage", ["transcribe", "diarize", "summarize", "mind_map", "embed"])
@pytest.mark.parametrize(
    ("returned_model", "message"),
    [(None, "returned no model"), ("wrong-model", "different model")],
)
def test_remote_stage_requires_exact_returned_model(
    monkeypatch, stage, returned_model, message
):
    import localplaud.worker.pipeline as pipeline

    class FakeClient:
        def submit_and_wait(self, request, timeout):
            payload = {} if returned_model is None else {"model": returned_model}
            return SimpleNamespace(artifacts={"result.json": json.dumps(payload).encode()})

        def close(self):
            pass

    monkeypatch.setattr(
        pipeline.RemoteWorkerClient, "from_config", lambda _config: FakeClient()
    )
    snapshot = {
        "stages": {
            stage: {
                "connection": "worker:test",
                "model": "requested-model",
                "execution_target": "remote_worker",
                "configuration": {"base_url": "https://worker.example/"},
            }
        }
    }

    with pytest.raises(ValueError, match=message):
        pipeline._run_remote_stage(
            "recording", snapshot, stage, [pipeline._remote_json_input("transcript", {})]
        )


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


@pytest.mark.parametrize(
    ("returned_model", "message"),
    [(None, "returned no model"), ("wrong-space", "different model")],
)
def test_pipeline_remote_embedding_requires_exact_returned_model(
    monkeypatch, tmp_path, returned_model, message
):
    import localplaud.db.session as db_session
    import localplaud.worker.pipeline as pipeline
    from localplaud.config import get_settings
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import init_db, session_scope

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'embed.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()
    with session_scope() as session:
        session.add(PlaudFile(id="remote-embed"))
    snapshot = {
        "stages": {
            "embed": {
                "connection": "worker:gpu",
                "model": "requested-space",
                "execution_target": "remote_worker",
            }
        }
    }
    token = pipeline._PROFILE_SNAPSHOT.set(snapshot)
    try:
        with pytest.raises(ValueError, match=message):
            pipeline._persist_remote_chunks(
                "remote-embed",
                {"chunks": [], "vectors_base64": [], "model": returned_model, "dim": 1},
            )
    finally:
        pipeline._PROFILE_SNAPSHOT.reset(token)
def test_remote_provider_timeouts_are_bounded_below_dispatch_lease():
    from localplaud.remote.client import (
        MAX_PROVIDER_TIMEOUT_SECONDS,
        validate_provider_timeout,
    )

    assert validate_provider_timeout(MAX_PROVIDER_TIMEOUT_SECONDS, field="job_timeout") == (
        MAX_PROVIDER_TIMEOUT_SECONDS
    )
    with pytest.raises(ValueError, match="no more than"):
        validate_provider_timeout(
            MAX_PROVIDER_TIMEOUT_SECONDS + 1,
            field="job_timeout",
        )
