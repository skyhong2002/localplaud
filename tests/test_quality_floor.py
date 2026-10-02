"""Quality policy remains durable and excludes unqualified fallback execution."""
import pytest
from sqlalchemy import create_engine, text

from localplaud.db.migrations import migrate_quality_floor_schema
from localplaud.providers.contracts import Capability, StageCapabilities
from localplaud.providers.fallback import candidate_snapshots
from localplaud.providers.resolver import ResolutionError, resolve_profile


def resolve(quality, floor=0.8):
    catalog = {
        ("local", name): Capability(
            execution_target="local", data_egress=False,
            stages=(StageCapabilities(stage="transcribe", quality=score),),
        ) for name, score in (("primary", 1), ("alternate", quality))
    }
    layer = {
        "policy": {"no_egress": True, "quality_floor": {"transcribe": floor},
                   "fallback_policy": {"stages": {"transcribe": [
                       {"connection": "local", "model": "alternate"}]}}},
        "stages": {"transcribe": {"connection": "local", "model": "primary"}},
    }
    return resolve_profile([layer], catalog)


@pytest.mark.parametrize("quality", [0.7, None])
def test_reject_lower_or_unknown_quality_in_immutable_snapshot(quality):
    result = resolve(quality)
    snapshot = result.to_dict()
    check = snapshot["policy"]["fallback_policy"]["stages"]["transcribe"][0]["quality_resolution"]
    assert check["accepted"] is False
    assert "transcribe fallback 1 rejected" in check["reason"]
    assert len(candidate_snapshots(snapshot, "transcribe")) == 1
    snapshot["policy"]["quality_floor"]["transcribe"] = 0
    assert result.to_dict()["policy"]["quality_floor"]["transcribe"] == 0.8


def test_equal_floor_is_eligible_and_legacy_no_floor_is_compatible():
    assert len(candidate_snapshots(resolve(0.8).to_dict(), "transcribe")) == 2
    assert len(candidate_snapshots(resolve(None, floor=0).to_dict(), "transcribe")) == 1


@pytest.mark.parametrize("floor", [-1, 2, float("nan"), True, "high"])
def test_invalid_floor_is_rejected(floor):
    with pytest.raises(ResolutionError, match="quality floor"):
        resolve(1, floor)


def test_additive_upgrade_preserves_existing_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE execution_profiles (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(text("INSERT INTO execution_profiles VALUES (1, 'existing')"))
    assert migrate_quality_floor_schema(engine) == ["execution_profiles.quality_floor"]
    assert migrate_quality_floor_schema(engine) == []
    with engine.connect() as connection:
        assert connection.execute(text("SELECT name, quality_floor FROM execution_profiles")).one() == (
            "existing", "{}")


def test_preview_api_and_durable_profile(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    import localplaud.db.session as db_session
    from localplaud.api.app import app
    from localplaud.config import get_settings
    from localplaud.db.models import ExecutionProfile
    from localplaud.db.session import session_scope

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'policy.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    db_session.init_db()
    with session_scope() as session:
        profile = session.scalar(select(ExecutionProfile).where(ExecutionProfile.is_system_default))
        profile.quality_floor = {"transcribe": 0.9}
    client = TestClient(app)
    base = client.post("/api/providers/resolve", json={}).json()["resolved"]
    selection = base["stages"]["transcribe"]
    # Use a distinct model for the fallback; the seeded align model also supports transcribe.
    from localplaud.db.models import ModelCatalogEntry, ProviderConnection
    with session_scope() as session:
        connection = session.scalar(select(ProviderConnection).where(ProviderConnection.key == selection["connection"]))
        model = session.scalar(select(ModelCatalogEntry).where(ModelCatalogEntry.connection_id == connection.id, ModelCatalogEntry.model_key == selection["model"]))
        session.add(ModelCatalogEntry(connection_id=connection.id, model_key="unrated", display_name="Unrated", capabilities=model.capabilities))
    response = client.post("/api/providers/resolve", json={"recording": {
        "policy": {"fallback_policy": {"stages": {"transcribe": [{
            "connection": selection["connection"], "model": "unrated"}]}}}}})
    assert response.status_code == 200
    snapshot = response.json()["resolved"]
    assert snapshot["policy"]["quality_floor"] == {"transcribe": 0.9}
    check = snapshot["policy"]["fallback_policy"]["stages"]["transcribe"][0]["quality_resolution"]
    assert not check["accepted"] and "unknown" in check["reason"]
