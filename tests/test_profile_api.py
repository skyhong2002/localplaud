from __future__ import annotations


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'profiles.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def test_profile_catalog_resolution_and_recording_override(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    profiles = client.get("/api/execution-profiles").json()["profiles"]
    assert len(profiles) == 7
    assert {profile["name"] for profile in profiles} >= {
        "apple-local", "nvidia-local", "cpu-local", "openai-cloud",
        "openai-compatible", "remote-gpu",
    }
    assert sum(profile["system_default"] for profile in profiles) == 1

    payload = {
        "name": "private-mlx",
        "stages": {"transcribe": {"provider": "mlx-whisper",
                                  "model": "mlx-community/whisper-large-v3-turbo",
                                  "egress": "local_only"}},
        "policy": {"no_egress": True, "allowed_providers": ["mlx-whisper"]},
    }
    created = client.post("/api/execution-profiles", json=payload)
    assert created.status_code == 201

    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="r1", filename="recording"))
    selected = client.put(
        "/api/files/r1/execution-profile", json={"profile_id": created.json()["id"]}
    )
    assert selected.status_code == 200
    resolved = client.get("/api/execution-profiles/resolve?stage=transcribe&file_id=r1")
    assert resolved.status_code == 200
    assert resolved.json()["route"]["provider"] == "mlx-whisper"
    assert resolved.json()["layers"][-1]["source"] == "recording_override"

    folder = client.post("/api/execution-profiles", json={
        "name": "folder-layer", "stages": {"transcribe": {"options": {"language": "en"}}},
        "policy": {},
    })
    template = client.post("/api/execution-profiles", json={
        "name": "template-layer", "stages": {"transcribe": {"options": {"language": "zh"}}},
        "policy": {},
    })
    assert folder.status_code == template.status_code == 201
    context = client.put("/api/files/r1/profile-context", json={
        "folder_profile_id": folder.json()["id"],
        "template_profile_id": template.json()["id"],
    })
    assert context.status_code == 200
    layered = client.get("/api/execution-profiles/resolve?stage=transcribe&file_id=r1").json()
    assert [item["source"] for item in layered["layers"]] == [
        "system_default", "folder_rule", "template_default", "recording_override"
    ]
    assert layered["route"]["options"]["language"] == "zh"

    assert client.get("/api/provider-connections").json()["connections"]
    assert client.get("/api/provider-models").json()["models"]
    assert client.get("/api/capabilities").status_code == 200
    hardware = client.get("/api/hardware")
    assert hardware.status_code == 200
    assert hardware.json()["recommendations"]

    with session_scope() as session:
        session.add(PlaudFile(id="starter", filename="starter"))
    by_name = {profile["name"]: profile for profile in profiles}
    assert client.put("/api/files/starter/execution-profile", json={
        "profile_id": by_name["openai-compatible"]["id"],
    }).status_code == 200
    compatible = client.get(
        "/api/execution-profiles/resolve?stage=summarize&file_id=starter"
    )
    assert compatible.status_code == 200
    assert compatible.json()["route"]["connection"] == "openai-compatible"
    assert "OPENAI_COMPATIBLE_API_KEY" not in compatible.text

    assert client.put("/api/files/starter/execution-profile", json={
        "profile_id": by_name["remote-gpu"]["id"],
    }).status_code == 200
    remote = client.get("/api/execution-profiles/resolve?stage=diarize&file_id=starter")
    assert remote.status_code == 200
    assert remote.json()["route"]["provider"] == "remote-worker"
    assert remote.json()["route"]["model"] == "pyannote-community-1"


def test_profile_api_rejects_inline_secrets_and_policy_violation(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    rejected = client.post("/api/execution-profiles", json={
        "name": "leaky", "stages": {"transcribe": {"provider": "openai",
        "api_key": "secret"}},
    })
    assert rejected.status_code == 400
    rejected_header = client.post("/api/execution-profiles", json={
        "name": "leaky-header", "stages": {"summarize": {"provider": "openai",
        "headers": {"Authorization": "Bearer secret"}}},
    })
    assert rejected_header.status_code == 400

    bad = client.post("/api/execution-profiles", json={
        "name": "external-under-private-policy",
        "stages": {"transcribe": {"provider": "openai", "egress": "external"}},
        "policy": {"no_egress": True},
    })
    assert bad.status_code == 422
    assert any(term in bad.json()["error"] for term in ("no-egress", "not allowed"))

    connection = client.post("/api/provider-connections", json={
        "name": "cloud-audio", "provider_type": "openai",
        "base_url": "https://8.8.8.8/v1", "secret_ref": "env:TEST_OPENAI_KEY",
    })
    assert connection.status_code == 201
    model = client.post("/api/provider-models", json={
        "connection_id": connection.json()["id"], "name": "audio-model",
        "capabilities": ["asr"],
    })
    assert model.status_code == 201
    connected_bad = client.post("/api/execution-profiles", json={
        "name": "external-connection-under-private-policy",
        "stages": {"transcribe": {"provider": "openai", "connection": "cloud-audio",
                                    "model": "audio-model"}},
        "policy": {"no_egress": True},
    })
    assert connected_bad.status_code == 422
    assert "no-egress" in connected_bad.json()["error"]


def test_connection_secrets_are_references_and_masked(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    created = client.post("/api/provider-connections", json={
        "name": "custom-text", "provider_type": "openai",
        "base_url": "http://127.0.0.1:9999/v1",
        "secret_ref": "env:MY_PRIVATE_MODEL_KEY", "configuration": {},
    })
    assert created.status_code == 201
    assert created.json()["secret_ref"].startswith("env:****")
    assert "MY_PRIVATE_MODEL_KEY" not in created.text
    rows = client.get("/api/provider-connections").json()["connections"]
    row = next(item for item in rows if item["name"] == "custom-text")
    assert row["secret_ref"].startswith("env:****")
    health = client.post(f"/api/provider-connections/{row['id']}/health?capability=asr")
    assert health.status_code == 422  # capability must be declared by a model first


def test_profile_api_rejects_malformed_cost_ssrf_and_secret_variants(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    malformed = client.post("/api/execution-profiles", json={
        "name": "malformed", "stages": {"transcribe": []}, "policy": {},
    })
    assert malformed.status_code == 422
    # Send the non-standard numeric token as raw JSON so the request reaches
    # the API instead of being rejected first by httpx's strict serializer.
    nan_cost = client.post(
        "/api/execution-profiles",
        content=(
            '{"name":"nan","stages":{"transcribe":{"estimated_cost":NaN}},'
            '"policy":{"cost_ceiling":1}}'
        ),
        headers={"content-type": "application/json"},
    )
    assert nan_cost.status_code == 422
    ssrf = client.post("/api/provider-connections", json={
        "name": "metadata", "provider_type": "openai",
        "base_url": "http://169.254.169.254/latest/meta-data", "configuration": {},
    })
    assert ssrf.status_code == 400
    secret = client.post("/api/provider-connections", json={
        "name": "secret", "provider_type": "openai",
        "configuration": {"credentials": {"apiKey": "sk-live-secret"}},
    })
    assert secret.status_code == 400
