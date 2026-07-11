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
    migrate_profile_snapshot_columns,
    migrate_stage_run_snapshot_column,
)
from localplaud.db.models import Base, ExecutionProfile, PlaudFile, StageRun
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
        assert len(list_connections(session)) == 4
        assert len(list_models(session)) == 4
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
        assert "Providers &amp; execution profiles" in settings_page.text


def test_provider_crud_api_rejects_secrets_and_validates_profiles(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'crud.db'}")
    config.get_settings(reload=True)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
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


def test_stage_run_snapshot_roundtrip(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(PlaudFile(id="r", filename="r"))
        session.add(StageRun(file_id="r", stage="transcribe",
                             resolved_profile_snapshot={"version": 1}))
        session.commit()
        assert session.query(StageRun).one().resolved_profile_snapshot == {"version": 1}
