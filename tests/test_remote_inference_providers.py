import hashlib
import json
from types import SimpleNamespace

from localplaud.config import RemoteEmbeddingsConfig, RemoteTextConfig
from localplaud.embeddings.remote_worker import RemoteWorkerEmbedder
from localplaud.llm.remote_worker import RemoteWorkerLLM
from localplaud.providers.remote_worker import ArtifactManifest, WorkerJobStatus


def _install_client(monkeypatch, payload, capability, model):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    artifact = ArtifactManifest(content_type="application/json", size_bytes=len(encoded),
                                sha256=hashlib.sha256(encoded).hexdigest(), payload=payload)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def handshake(self):
            return SimpleNamespace(capabilities=[SimpleNamespace(name=capability, models=[model])])

        def submit(self, request):
            assert request.input is None
            return WorkerJobStatus(job_id=request.job_id, idempotency_key=request.idempotency_key,
                                   state="completed", progress=1, artifact=artifact)

        def close(self):
            pass

    monkeypatch.setattr("localplaud.providers.remote_inference.RemoteWorkerClient", Client)


def test_remote_text_and_embeddings_verify_json_artifacts(monkeypatch):
    _install_client(monkeypatch, {"text": "grounded", "usage": {"input_tokens": 4}},
                    "notes", "text-model")
    llm = RemoteWorkerLLM(RemoteTextConfig(base_url="https://worker.example", token="t",
                                           model="text-model", capability="notes"))
    assert llm.complete("prompt") == "grounded"
    assert llm.usage["input_tokens"] == 4

    _install_client(monkeypatch, {"vectors": [[1.0, 0.0], [0.0, 1.0]]},
                    "embeddings", "embed-model")
    embedder = RemoteWorkerEmbedder(RemoteEmbeddingsConfig(
        base_url="https://worker.example", token="t", model="embed-model", dim=2
    ))
    assert embedder.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
