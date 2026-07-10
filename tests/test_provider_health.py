from types import SimpleNamespace


def test_remote_worker_health_uses_authenticated_handshake(monkeypatch):
    from localplaud.providers.remote_worker import WorkerCapability

    seen = {}

    class Client:
        def __init__(self, base_url, token, timeout):
            seen.update(base_url=base_url, token=token, timeout=timeout)

        def handshake(self):
            return SimpleNamespace(
                protocol_version="1", worker_id="gpu-1",
                capabilities=[WorkerCapability(name="asr", models=["turbo"])],
                max_concurrent_jobs=2, runtime={"device": "cuda", "memory_mib": 24576},
            )

        def close(self):
            seen["closed"] = True

    monkeypatch.setenv("TEST_WORKER_TOKEN", "worker-secret")
    monkeypatch.setattr("localplaud.providers.remote_worker.RemoteWorkerClient", Client)
    from localplaud.providers.health import check_provider

    state, detail = check_provider(
        "remote-worker", "env:TEST_WORKER_TOKEN", "https://worker.example"
    )
    assert state == "healthy"
    assert seen == {"base_url": "https://worker.example", "token": "worker-secret",
                    "timeout": 10, "closed": True}
    assert detail["worker_id"] == "gpu-1"
    assert detail["capabilities"][0]["name"] == "asr"
    assert detail["runtime"]["device"] == "cuda"


def test_remote_worker_health_fails_closed_without_secret(monkeypatch):
    monkeypatch.delenv("MISSING_WORKER_TOKEN", raising=False)
    from localplaud.providers.health import check_provider

    state, detail = check_provider(
        "remote-worker", "env:MISSING_WORKER_TOKEN", "https://worker.example"
    )
    assert state == "unavailable"
    assert "not available" in detail["note"]
