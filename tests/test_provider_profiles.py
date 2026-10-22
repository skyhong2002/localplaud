import pytest

from localplaud.db.models import ExecutionProfile, StageName
from localplaud.providers.contracts import CapabilityKind, CapabilitySpec, EgressPolicy
from localplaud.providers.resolver import ProfileResolutionError, resolve_profile


def profile(name, *, stages=None, policy=None, enabled=True, version=1):
    return ExecutionProfile(name=name, stages=stages or {}, policy=policy or {},
                            enabled=enabled, version=version)


def test_capabilities_are_stage_specific_and_serializable():
    spec = CapabilitySpec(CapabilityKind.asr, languages=("zh", "en"),
                          word_timestamps=True, egress=EgressPolicy.external)
    assert spec.supports_language("zh")
    assert not spec.supports_language("ja")
    assert spec.to_dict()["kind"] == CapabilityKind.asr


def test_profile_resolution_order_and_snapshot():
    default = profile("default", stages={"transcribe": {"provider": "mlx-whisper", "model": "turbo",
                                                         "egress": "local_only",
                                                         "options": {"language": "auto"}}},
                      policy={"no_egress": True, "allowed_providers": ["mlx-whisper"]})
    template = profile("zh-template", stages={"transcribe": {"options": {"language": "zh"}}})
    resolved = resolve_profile(StageName.transcribe, system_default=default,
                               template_default=template)
    assert resolved["route"]["provider"] == "mlx-whisper"
    assert resolved["route"]["options"]["language"] == "zh"
    assert [x["source"] for x in resolved["layers"]] == ["system_default", "template_default"]


def test_no_egress_policy_rejects_external_route_and_fallback():
    external = profile("bad", stages={"transcribe": {"provider": "openai", "egress": "external"}},
                       policy={"no_egress": True})
    with pytest.raises(ProfileResolutionError, match="no-egress"):
        resolve_profile("transcribe", system_default=external)

    fallback = profile("bad-fallback", stages={"transcribe": {
        "provider": "mlx-whisper", "egress": "local_only",
        "fallback": [{"provider": "openai", "egress": "external"}],
    }}, policy={"no_egress": True})
    with pytest.raises(ProfileResolutionError, match="fallback"):
        resolve_profile("transcribe", system_default=fallback)


def test_allowed_provider_policy_applies_to_fallbacks_and_cost():
    fallback = profile("bad", stages={"transcribe": {
        "provider": "mlx-whisper", "egress": "local_only",
        "fallback": [{"provider": "openai", "egress": "external"}],
    }}, policy={"allowed_providers": ["mlx-whisper"]})
    with pytest.raises(ProfileResolutionError, match="fallback providers"):
        resolve_profile("transcribe", system_default=fallback)

    expensive = profile("expensive", stages={"summarize": {
        "provider": "openai", "egress": "external", "estimated_cost": 2.0,
    }}, policy={"cost_ceiling": 1.0})
    with pytest.raises(ProfileResolutionError, match="cost ceiling"):
        resolve_profile("summarize", system_default=expensive)

    expensive_fallback = profile("expensive-fallback", stages={"summarize": {
        "provider": "ollama", "estimated_cost": 0,
        "fallback": [{"provider": "openai", "estimated_cost": 2.0}],
    }}, policy={"cost_ceiling": 1.0})
    with pytest.raises(ProfileResolutionError, match="fallback 1.*cost ceiling"):
        resolve_profile("summarize", system_default=expensive_fallback)


def test_quality_floor_rejects_missing_speaker_capability():
    profile_row = profile("quality", stages={"transcribe": {
        "provider": "mlx-whisper", "model": "turbo", "egress": "local_only",
    }}, policy={"quality_floor": {"transcribe": {"word_timestamps": True,
                                                   "speaker_output": True}}})
    with pytest.raises(ProfileResolutionError, match="speaker_output"):
        resolve_profile("transcribe", system_default=profile_row)

    bad_fallback = profile("quality-fallback", stages={"transcribe": {
        "provider": "mlx-whisper",
        "fallback": [{"provider": "whispercpp"}],
    }}, policy={"quality_floor": {"transcribe": {"word_timestamps": True}}})
    with pytest.raises(ProfileResolutionError, match="fallback 1.*word_timestamps"):
        resolve_profile("transcribe", system_default=bad_fallback)


def test_retry_and_timeout_policy_is_validated_and_applies_to_fallbacks():
    bad_retry = profile("bad-retry", stages={"summarize": {"provider": "ollama"}},
                        policy={"retry": {"max_attempts": 0}})
    with pytest.raises(ProfileResolutionError, match="retry.max_attempts"):
        resolve_profile("summarize", system_default=bad_retry)

    bad_timeout = profile("bad-timeout", stages={"summarize": {
        "provider": "ollama", "fallback": [
            {"provider": "openai", "timeout_seconds": 61}
        ],
    }}, policy={"timeout_ceiling_seconds": 60})
    with pytest.raises(ProfileResolutionError, match="fallback 1.*timeout ceiling"):
        resolve_profile("summarize", system_default=bad_timeout)

    bounded = profile("bounded", stages={"summarize": {
        "provider": "ollama", "fallback": [{"provider": "openai"}],
    }}, policy={"timeout_ceiling_seconds": 45})
    resolved = resolve_profile("summarize", system_default=bounded)
    assert resolved["route"]["timeout_seconds"] == 45
    assert resolved["route"]["fallback"][0]["timeout_seconds"] == 45


def test_lower_layer_cannot_weaken_system_privacy_boundary():
    system = profile("system", stages={"transcribe": {
        "provider": "mlx-whisper", "model": "turbo",
    }}, policy={"no_egress": True, "allowed_providers": ["mlx-whisper"]})
    override = profile("override", stages={"transcribe": {
        "provider": "openai", "connection": "cloud", "model": "audio",
    }}, policy={"no_egress": False, "allowed_providers": ["openai"]})
    with pytest.raises(ProfileResolutionError, match="not allowed"):
        resolve_profile("transcribe", system_default=system, recording_override=override)


def test_explicit_legacy_sync_updates_only_system_default(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import Settings, get_settings
    from localplaud.db.session import init_db, session_scope
    from localplaud.providers.bootstrap import sync_system_default_profile

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'profiles.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()
    with session_scope() as session:
        default = session.query(ExecutionProfile).filter_by(is_system_default=True).one()
        user = ExecutionProfile(
            name="my-profile",
            stages={"transcribe": {"provider": "faster-whisper", "model": "mine"}},
            policy={"no_egress": True},
        )
        session.add(user)
        session.flush()
        user_id = user.id
        default.stages = {
            **default.stages,
            "transcribe": {"provider": "mlx-whisper", "model": "mlx-model"},
        }
        before = resolve_profile(StageName.transcribe, system_default=default)
        assert before["route"]["provider"] == "mlx-whisper"
        assert before["route"]["model"] == "mlx-model"

    changed = Settings(asr={
        "provider": "openai",
        "openai": {"model": "gpt-4o-transcribe"},
    })
    with session_scope() as session:
        synced = sync_system_default_profile(session, changed)
        resolved = resolve_profile(StageName.transcribe, system_default=synced)
        assert resolved["route"]["provider"] == "openai"
        assert resolved["route"]["model"] == "gpt-4o-transcribe"
        user = session.get(ExecutionProfile, user_id)
        assert user.stages == {
            "transcribe": {"provider": "faster-whisper", "model": "mine"}
        }
        assert user.policy == {"no_egress": True}


def test_synced_openai_default_materializes_common_api_key(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import Settings, get_settings
    from localplaud.db.session import init_db, session_scope
    from localplaud.providers.bootstrap import sync_system_default_profile
    from localplaud.providers.materialize import materialize_snapshot
    from localplaud.providers.routing import settings_for_route

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "common-openai-key")
    monkeypatch.delenv("LOCALPLAUD_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'openai.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()
    settings = Settings(asr={
        "provider": "openai",
        "openai": {"model": "gpt-4o-mini-transcribe"},
    })
    with session_scope() as session:
        profile = sync_system_default_profile(session, settings)
        snapshot = materialize_snapshot(
            session, resolve_profile(StageName.transcribe, system_default=profile)
        )
        assert snapshot["route"]["connection"] == "openai"
        assert snapshot["route"]["secret_ref"] == "env:OPENAI_API_KEY"
    routed = settings_for_route(settings, StageName.transcribe, snapshot)
    assert routed.asr.openai.api_key == "common-openai-key"
