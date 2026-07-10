import hashlib
import json
import time
from types import SimpleNamespace

from localplaud.asr.remote_worker_provider import RemoteWorkerAsrProvider
from localplaud.config import AsrConfig, RemoteWorkerAsrConfig
from localplaud.providers.remote_worker import ArtifactManifest, WorkerJobStatus


def test_remote_asr_dispatches_minimal_signed_input_and_verifies_artifact(monkeypatch, tmp_path):
    audio_dir = tmp_path / "recording-1"
    audio_dir.mkdir()
    audio = audio_dir / "audio.wav"
    audio.write_bytes(b"RIFF audio")
    transcript = {"language": "zh", "has_speakers": True, "segments": [{
        "text": "你好", "start": 0, "end": 1, "speaker": "SPEAKER_00",
        "words": [{"text": "你好", "start": 0, "end": 1, "speaker": "SPEAKER_00"}],
    }]}
    encoded = json.dumps(transcript, sort_keys=True, separators=(",", ":")).encode()
    artifact = ArtifactManifest(content_type="application/json", size_bytes=len(encoded),
                                sha256=hashlib.sha256(encoded).hexdigest(), payload=transcript)
    seen = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def handshake(self):
            return SimpleNamespace(capabilities=[SimpleNamespace(name="asr",
                                                                  models=["turbo"])])

        def submit(self, request):
            seen["request"] = request
            return WorkerJobStatus(job_id=request.job_id, idempotency_key=request.idempotency_key,
                                   state="completed", progress=1, artifact=artifact)

        def close(self):
            pass

    monkeypatch.setattr("localplaud.asr.remote_worker_provider.RemoteWorkerClient", FakeClient)
    cfg = AsrConfig(provider="remote-worker", remote_worker=RemoteWorkerAsrConfig(
        base_url="https://worker.example", token="worker-secret", model="turbo",
        input_base_url="https://localplaud.example", input_signing_secret="input-secret",
    ))
    result = RemoteWorkerAsrProvider(cfg).transcribe(audio, language="zh")
    assert result.text == "你好" and result.has_speakers
    request = seen["request"]
    assert request.input.kind == "signed_url"
    assert request.input.location.startswith("https://localplaud.example/api/worker-input/recording-1")
    assert "worker-secret" not in request.model_dump_json()
    assert "plaud" not in request.options


def test_worker_input_url_is_expiring_file_scoped(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.api.app import app
    from localplaud.asr.remote_worker_provider import sign_input
    from localplaud.config import get_settings
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import init_db, session_scope

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'worker.db'}")
    monkeypatch.setenv("LOCALPLAUD_ASR__REMOTE_WORKER__INPUT_SIGNING_SECRET", "input-secret")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    sha = hashlib.sha256(b"audio").hexdigest()
    with session_scope() as session:
        session.add(PlaudFile(id="r1", wav_path=str(audio)))
    expires = int(time.time()) + 60
    signature = sign_input("r1", expires, sha, "input-secret")
    client = TestClient(app)
    response = client.get(f"/api/worker-input/r1?expires={expires}&sha256={sha}&signature={signature}")
    assert response.status_code == 200 and response.content == b"audio"
    assert client.get(f"/api/worker-input/r1?expires={expires}&sha256={sha}&signature=bad").status_code == 403
