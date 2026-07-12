from types import MappingProxyType

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

import localplaud.config as config
import localplaud.db.session as db_session
from localplaud.api.app import app
from localplaud.config import Settings
from localplaud.db.migrations import (
    migrate_artifact_lineage_columns,
    migrate_legacy_provider_profile_schema,
    migrate_profile_snapshot_columns,
    migrate_stage_run_snapshot_column,
)
from localplaud.db.models import (
    Base,
    ExecutionProfile,
    PlaudFile,
    ProfileStageSelection,
    ProviderConnection,
    StageRun,
)
from localplaud.providers.contracts import Capability, ProviderStage, StageCapabilities
from localplaud.providers.resolver import ResolutionError, resolve_profile
from localplaud.providers.service import (
    bootstrap_default_profile,
    list_connections,
    list_models,
    list_profiles,
    resolve_recording_profile,
    select_recording_override,
)
from localplaud.worker.pipeline import _settings_for_stage


def _cap(*stages, egress=False):
    return Capability(
        execution_target="cloud" if egress else "local",
        data_egress=egress,
        stages=tuple(StageCapabilities(stage=stage) for stage in stages),
    )


def test_resolution_precedence_partial_merge_and_immutability():
    catalog = {("local", "one"): _cap(ProviderStage.summarize),
               ("local", "two"): _cap(ProviderStage.summarize)}
    resolved = resolve_profile([
        {"key": "system", "policy": {"no_egress": True, "cost_ceiling": 3},
         "stages": {"summarize": {"connection": "local", "model": "one",
                                    "options": {"temperature": 0.1, "language": "zh"}}}},
        {"key": "folder", "stages": {"summarize": {"options": {"temperature": 0.2}}}},
        {"key": "template", "policy": {"cost_ceiling": 2}},
        {"key": "recording", "stages": {"summarize": {"model": "two"}}},
    ], catalog)
    data = resolved.to_dict()
    assert data["stages"]["summarize"]["model"] == "two"
    assert data["stages"]["summarize"]["options"] == {"temperature": 0.2, "language": "zh"}
    assert data["policy"]["cost_ceiling"] == 2
    assert isinstance(resolved.snapshot, MappingProxyType)
    with pytest.raises(TypeError):
        resolved.snapshot["new"] = 1
    assert '"model":"two"' in resolved.to_json()


def test_resolution_rejects_egress_and_unsupported_stage():
    cloud = {("cloud", "model"): _cap(ProviderStage.ask, egress=True)}
    layer = {"policy": {"no_egress": True},
             "stages": {"ask": {"connection": "cloud", "model": "model"}}}
    with pytest.raises(ResolutionError, match="no-egress"):
        resolve_profile([layer], cloud)
    layer["policy"]["no_egress"] = False
    layer["stages"] = {"embed": {"connection": "cloud", "model": "model"}}
    with pytest.raises(ResolutionError, match="does not support"):
        resolve_profile([layer], cloud)


def test_models_bootstrap_and_services_are_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'providers.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first = bootstrap_default_profile(session, Settings())
        session.commit()
        first_id = first.id
    with Session(engine) as session:
        second = bootstrap_default_profile(session, Settings())
        session.commit()
        assert second.id == first_id
        assert len(list_connections(session)) == 5
        assert len(list_models(session)) == 5
        profiles = list_profiles(session)
        assert len(profiles) == 1
        assert set(profiles[0]["stages"]) == {stage.value for stage in ProviderStage}
        assert all(connection["secret_ref"] is None for connection in list_connections(session))
        session.add(PlaudFile(id="recording", filename="test"))
        session.flush()
        selected = select_recording_override(session, "recording", first_id,
                                             stages={"ask": {"options": {"x": 1}}})
        assert selected["profile_id"] == first_id
        resolved = resolve_recording_profile(session, "recording").to_dict()
        assert resolved["stages"]["ask"]["options"] == {"x": 1}
        assert resolved["layers"][-1] == "recording:recording"


def test_legacy_provider_profile_schema_rebuild_preserves_ids_and_config(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-providers.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.execute(text("""
            CREATE TABLE provider_connections (
                id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL UNIQUE,
                provider_type VARCHAR(64) NOT NULL, base_url VARCHAR(1024),
                secret_ref VARCHAR(256), configuration JSON NOT NULL,
                enabled BOOLEAN NOT NULL, version INTEGER NOT NULL,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE model_catalog_entries (
                id INTEGER PRIMARY KEY, connection_id INTEGER NOT NULL,
                model_key VARCHAR(256) NOT NULL, display_name VARCHAR(256) NOT NULL,
                capabilities JSON NOT NULL, enabled BOOLEAN NOT NULL,
                FOREIGN KEY(connection_id) REFERENCES provider_connections(id)
            )
        """))
        connection.execute(text("""
            CREATE TABLE execution_profiles (
                id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL UNIQUE,
                description TEXT, stages JSON NOT NULL, policy JSON NOT NULL,
                is_system_default BOOLEAN NOT NULL, enabled BOOLEAN NOT NULL,
                version INTEGER NOT NULL, created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE profile_stage_selections (
                id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL,
                stage VARCHAR(32) NOT NULL, connection_id INTEGER NOT NULL,
                model_id INTEGER NOT NULL, options JSON NOT NULL,
                FOREIGN KEY(profile_id) REFERENCES execution_profiles(id),
                FOREIGN KEY(connection_id) REFERENCES provider_connections(id),
                FOREIGN KEY(model_id) REFERENCES model_catalog_entries(id)
            )
        """))
        connection.execute(text("""
            INSERT INTO provider_connections VALUES (
                7, 'openai-cloud', 'openai', 'https://api.openai.com/v1',
                'env:OPENAI_API_KEY', '{"timeout": 45}', 1, 2,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            INSERT INTO model_catalog_entries VALUES (
                11, 7, 'gpt-test', 'GPT Test', '{}', 1
            )
        """))
        connection.execute(text("""
            INSERT INTO execution_profiles VALUES (
                3, 'system-default', NULL, '{}',
                '{"no_egress": false, "cost_ceiling": 2.5}',
                1, 1, 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
        """))
        connection.execute(text("""
            INSERT INTO profile_stage_selections VALUES (19, 3, 'ask', 7, 11, '{}')
        """))

    assert migrate_legacy_provider_profile_schema(engine) == [
        "provider_connections",
        "execution_profiles",
    ]
    assert migrate_legacy_provider_profile_schema(engine) == []
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    with Session(engine) as session:
        provider = session.get(ProviderConnection, 7)
        assert provider.key == "openai-cloud"
        assert provider.execution_target == "cloud" and provider.data_egress is True
        assert provider.secret_ref == "env:OPENAI_API_KEY"
        assert provider.config == {
            "timeout": 45,
            "base_url": "https://api.openai.com/v1",
        }
        profile = session.get(ExecutionProfile, 3)
        assert profile.key == "legacy-settings-default" and profile.version == 2
        assert profile.cost_ceiling == 2.5
        upgraded = bootstrap_default_profile(session, Settings())
        assert upgraded.id == 4
        assert upgraded.version == 3 and upgraded.is_system_default is True
        assert upgraded.no_egress is False
        correct = next(item for item in upgraded.stage_selections if item.stage == "correct")
        assert session.get(ProviderConnection, correct.connection_id).provider_type == "opencode-go"
        selection = session.get(ProfileStageSelection, 19)
        assert (selection.profile_id, selection.connection_id, selection.model_id) == (3, 7, 11)


def test_profile_key_can_have_multiple_versions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'profile-versions.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                ExecutionProfile(key="apple-local", name="Apple Local", version=1),
                ExecutionProfile(key="apple-local", name="Apple Local", version=2),
            ]
        )
        session.commit()
        assert session.query(ExecutionProfile).count() == 2


def test_no_egress_profile_disables_legacy_asr_fallback():
    settings = Settings(asr={"provider": "faster-whisper", "fallback": ["openai"]})
    snapshot = {
        "policy": {"no_egress": True, "fallback_policy": {"asr": ["openai"]}},
        "stages": {
            "transcribe": {
                "connection": "asr:faster-whisper",
                "model": "large-v3-turbo",
                "options": {},
            }
        },
    }
    resolved = _settings_for_stage(settings, snapshot, "transcribe")
    assert resolved.asr.provider == "faster-whisper"
    assert resolved.asr.fallback == []
    assert settings.asr.fallback == ["openai"]


def test_legacy_sqlite_column_migration_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE stage_runs (id INTEGER PRIMARY KEY)"))
    assert migrate_stage_run_snapshot_column(engine) is True
    assert migrate_stage_run_snapshot_column(engine) is False
    assert "resolved_profile_snapshot" in {
        column["name"] for column in inspect(engine).get_columns("stage_runs")
    }


def test_legacy_artifact_snapshot_migration(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-artifacts.db'}")
    with engine.begin() as connection:
        for table in ("transcripts", "summaries", "chunks"):
            connection.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
    assert migrate_profile_snapshot_columns(engine) == ["transcripts", "summaries", "chunks"]
    assert migrate_profile_snapshot_columns(engine) == []


def test_legacy_artifact_lineage_migration(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-lineage.db'}")
    with engine.begin() as connection:
        for table in ("summaries", "chunks"):
            connection.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
    migrated = migrate_artifact_lineage_columns(engine)
    assert len(migrated) == 6
    assert migrate_artifact_lineage_columns(engine) == []
    for table in ("summaries", "chunks"):
        columns = {column["name"] for column in inspect(engine).get_columns(table)}
        assert {
            "input_transcript_id",
            "input_transcript_revision",
            "input_transcript_source",
        } <= columns


def test_provider_read_api(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    config.get_settings(reload=True)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    with TestClient(app) as client:
        response = client.get("/api/providers/profiles")
        assert response.status_code == 200
        assert response.json()["profiles"][0]["key"] == "legacy-settings-default"
        preview = client.post("/api/providers/resolve", json={})
        assert preview.status_code == 200
        assert "transcribe" in preview.json()["resolved"]["stages"]
        settings_page = client.get("/settings")
        assert settings_page.status_code == 200
        assert "<h1 class=\"page\">Settings</h1>" in settings_page.text
        assert 'aria-label="Settings sections"' in settings_page.text


def test_provider_crud_api_rejects_secrets_and_validates_profiles(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'crud.db'}")
    config.get_settings(reload=True)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    monkeypatch.setattr(
        "localplaud.providers.service._probe_connection",
        lambda connection, model_key=None: (True, f"model {model_key or 'default'} ready"),
    )
    with TestClient(app) as client:
        rejected = client.post(
            "/api/providers/connections",
            json={
                "key": "llm:unsafe",
                "name": "Unsafe",
                "provider_type": "openai",
                "api_key": "must-not-be-stored",
            },
        )
        assert rejected.status_code == 422

        connection = client.post(
            "/api/providers/connections",
            json={
                "key": "llm:test-local",
                "name": "Test Local",
                "provider_type": "ollama",
                "execution_target": "local",
                "data_egress": False,
                "config": {},
            },
        )
        assert connection.status_code == 201
        connection_id = connection.json()["id"]
        edited_connection = client.put(
            f"/api/providers/connections/{connection_id}",
            json={
                "key": "llm:test-local",
                "name": "Renamed Local",
                "provider_type": "ollama",
                "execution_target": "local",
                "data_egress": False,
                "config": {},
            },
        )
        assert edited_connection.status_code == 200
        assert edited_connection.json()["name"] == "Renamed Local"
        health = client.post(f"/api/providers/connections/{connection_id}/health")
        assert health.json()["status"] == "healthy"

        capability = _cap(ProviderStage.summarize).model_dump(mode="json")
        model = client.post(
            "/api/providers/models",
            json={
                "connection_id": connection_id,
                "model_key": "test-model",
                "display_name": "Test Model",
                "capabilities": capability,
            },
        )
        assert model.status_code == 201
        edited_model = client.put(
            f"/api/providers/models/{model.json()['id']}",
            json={
                "connection_id": connection_id,
                "model_key": "test-model",
                "display_name": "Renamed Model",
                "capabilities": capability,
                "enabled": True,
            },
        )
        assert edited_model.status_code == 200
        assert edited_model.json()["display_name"] == "Renamed Model"
        model_health = client.post(f"/api/providers/models/{model.json()['id']}/health")
        assert model_health.json()["status"] == "healthy"

        profile = client.post(
            "/api/providers/profiles",
            json={
                "key": "test-local",
                "name": "Test Local",
                "no_egress": True,
                "privacy_policy": "local-only",
                "stages": {
                    "summarize": {
                        "connection": "llm:test-local",
                        "model": "test-model",
                        "options": {},
                    }
                },
            },
        )
        assert profile.status_code == 201
        profile_id = profile.json()["id"]
        assert client.delete(f"/api/providers/profiles/{profile_id}").status_code == 204
        assert client.delete(f"/api/providers/models/{model.json()['id']}").status_code == 204
        assert client.delete(f"/api/providers/connections/{connection_id}").status_code == 204


def test_remote_worker_connection_health_uses_handshake_and_checks_model(
    monkeypatch, tmp_path
):
    from localplaud.remote.protocol import HandshakeResponse, StageCapability

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'remote-health.db'}")
    monkeypatch.setenv("TEST_WORKER_TOKEN", "test-secret")
    config.get_settings(reload=True)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    seen = []
    worker_configs = []

    class FakeClient:
        def handshake(self):
            seen.append("handshake")
            return HandshakeResponse(
                worker_id="gpu-health",
                capabilities=[
                    StageCapability(stage="transcribe", models=["turbo"]),
                    StageCapability(stage="diarize", models=["community-1"]),
                ],
            )

        def close(self):
            seen.append("closed")

    monkeypatch.setattr(
        "localplaud.remote.client.RemoteWorkerClient.from_config",
        lambda worker_config: worker_configs.append(worker_config) or FakeClient(),
    )
    with TestClient(app) as client:
        connection = client.post(
            "/api/providers/connections",
            json={
                "key": "worker:health",
                "name": "Health worker",
                "provider_type": "localplaud-worker",
                "execution_target": "remote_worker",
                "data_egress": True,
                "secret_ref": "env:TEST_WORKER_TOKEN",
                "config": {"base_url": "https://worker.example/"},
            },
        ).json()
        health = client.post(
            f"/api/providers/connections/{connection['id']}/health"
        ).json()
        assert health["status"] == "healthy"
        assert "gpu-health" in health["detail"]
        assert "transcribe" in health["detail"]

        available = client.post(
            "/api/providers/models",
            json={
                "connection_id": connection["id"],
                "model_key": "turbo",
                "display_name": "Turbo",
                "capabilities": _cap(ProviderStage.transcribe, egress=True).model_dump(mode="json"),
            },
        ).json()
        available_health = client.post(
            f"/api/providers/models/{available['id']}/health"
        ).json()
        assert available_health["status"] == "healthy"
        assert available_health["detail"].endswith("stages transcribe")

        missing = client.post(
            "/api/providers/models",
            json={
                "connection_id": connection["id"],
                "model_key": "missing",
                "display_name": "Missing",
                "capabilities": _cap(ProviderStage.transcribe, egress=True).model_dump(mode="json"),
            },
        ).json()
        missing_health = client.post(
            f"/api/providers/models/{missing['id']}/health"
        ).json()
        assert missing_health["status"] == "degraded"
        assert "not advertised" in missing_health["detail"]
    assert seen == ["handshake", "closed"] * 3
    assert all(item["base_url"] == "https://worker.example/" for item in worker_configs)
    assert all(item["token_env"] == "TEST_WORKER_TOKEN" for item in worker_configs)
    assert "test-secret" not in str(worker_configs)


def test_stage_run_snapshot_roundtrip(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(PlaudFile(id="r", filename="r"))
        session.add(StageRun(file_id="r", stage="transcribe",
                             resolved_profile_snapshot={"version": 1}))
        session.commit()
        assert session.query(StageRun).one().resolved_profile_snapshot == {"version": 1}
