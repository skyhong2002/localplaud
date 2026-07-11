from types import MappingProxyType

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

import localplaud.config as config
import localplaud.db.session as db_session
from localplaud.api.app import app
from localplaud.config import Settings
from localplaud.db.migrations import migrate_stage_run_snapshot_column
from localplaud.db.models import Base, PlaudFile, StageRun
from localplaud.providers.contracts import Capability, ProviderStage, StageCapabilities
from localplaud.providers.resolver import ResolutionError, resolve_profile
from localplaud.providers.service import (
    bootstrap_default_profile,
    list_connections,
    list_models,
    list_profiles,
    select_recording_override,
)


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


def test_legacy_sqlite_column_migration_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE stage_runs (id INTEGER PRIMARY KEY)"))
    assert migrate_stage_run_snapshot_column(engine) is True
    assert migrate_stage_run_snapshot_column(engine) is False
    assert "resolved_profile_snapshot" in {
        column["name"] for column in inspect(engine).get_columns("stage_runs")
    }


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


def test_stage_run_snapshot_roundtrip(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'snapshot.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(PlaudFile(id="r", filename="r"))
        session.add(StageRun(file_id="r", stage="transcribe",
                             resolved_profile_snapshot={"version": 1}))
        session.commit()
        assert session.query(StageRun).one().resolved_profile_snapshot == {"version": 1}
