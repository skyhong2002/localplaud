"""Upgrade-compatibility and retention safety found in the merged-release review."""
from types import SimpleNamespace

from localplaud import storage_usage
from localplaud.providers.contracts import Capability, StageCapabilities
from localplaud.providers.resolver import resolve_profile
from localplaud.worker.pipeline import _profile_stage_matches


def _resolve(floor=None):
    catalog = {
        ("local", name): Capability(
            execution_target="local", data_egress=False,
            stages=(StageCapabilities(stage="correct", quality=0.9),),
        ) for name in ("primary", "alternate")
    }
    policy = {"no_egress": True, "fallback_policy": {"stages": {"correct": [
        {"connection": "local", "model": "alternate"}]}}}
    if floor is not None:
        policy["quality_floor"] = {"correct": floor}
    layer = {"policy": policy, "stages": {"correct": {"connection": "local", "model": "primary"}}}
    return resolve_profile([layer], catalog).to_dict()


def test_profiles_without_a_floor_resolve_exactly_as_before_the_floor_existed():
    snapshot = _resolve()
    assert "quality_floor" not in snapshot["policy"]
    assert "quality_resolution" not in snapshot["policy"]["fallback_policy"]["stages"]["correct"][0]


def test_fallback_artifacts_stay_reusable_after_a_floor_is_configured():
    from localplaud.providers.fallback import candidate_snapshots

    before = _resolve()
    after = _resolve(floor=0.5)
    # The stored snapshot of an artifact produced by the fallback before the upgrade.
    produced_by_fallback = candidate_snapshots(before, "correct")[1]
    assert _profile_stage_matches(produced_by_fallback, after, "correct")
    assert _profile_stage_matches(before, after, "correct")


def _plan(monkeypatch, keep, backups):
    monkeypatch.setattr("localplaud.backups.list_workspace_backups", lambda: backups)
    session = SimpleNamespace(
        get=lambda model, key: SimpleNamespace(value={"backup_keep_latest": keep})
    )
    return [item["name"] for item in storage_usage.retention_plan(session)["backups_to_delete"]]


def test_retention_keeps_the_newest_media_archive_and_ignores_invalid_limits(monkeypatch):
    backups = [
        {"name": "db-3", "media": {"included": False}},
        {"name": "db-2", "media": {"included": False}},
        {"name": "full-1", "media": {"included": True}},
        {"name": "full-0", "media": {"included": True}},
        {"name": "broken", "status": "invalid"},
    ]
    assert _plan(monkeypatch, 2, backups) == ["full-0", "broken"]
    for invalid in (0, -1, True, "2", 1000):
        assert _plan(monkeypatch, invalid, backups) == []
