from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

import localplaud.config as config
import localplaud.db.session as db_session
from localplaud.api.app import app
from localplaud.config import Settings
from localplaud.db.migrations import (
    migrate_artifact_lineage_columns,
    migrate_legacy_provider_profile_schema,
    migrate_legacy_summary_schema,
    migrate_profile_resolution_schema,
    migrate_profile_snapshot_columns,
    migrate_stage_run_snapshot_column,
)
from localplaud.db.models import (
    AutomationRule,
    Base,
    Chunk,
    ExecutionProfile,
    Folder,
    ModelCatalogEntry,
    NoteTemplate,
    PlaudFile,
    ProfileStageSelection,
    ProviderConnection,
    RecordingProfileOverride,
    RecordingRuleProfileAssignment,
    StageName,
    StageRun,
    StageStatus,
)
from localplaud.providers.contracts import Capability, ProviderStage, StageCapabilities
from localplaud.providers.resolver import ResolutionError, resolve_profile
from localplaud.providers.service import (
    ProfileMutationBusyError,
    bootstrap_default_profile,
    clear_recording_override,
    create_profile_version,
    delete_profile,
    list_connections,
    list_models,
    list_profiles,
    lock_library_profile_resolution,
    preview_resolution,
    resolve_recording_profile,
    save_connection,
    save_model,
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
    catalog = {
        ("local", "one"): _cap(ProviderStage.summarize),
        ("local", "two"): _cap(ProviderStage.summarize),
    }
    resolved = resolve_profile(
        [
            {
                "key": "system",
                "policy": {"no_egress": True, "cost_ceiling": 3},
                "stages": {
                    "summarize": {
                        "connection": "local",
                        "model": "one",
                        "options": {"temperature": 0.1, "language": "zh"},
                    }
                },
            },
            {"key": "folder", "stages": {"summarize": {"options": {"temperature": 0.2}}}},
            {"key": "template", "policy": {"cost_ceiling": 2}},
            {"key": "recording", "stages": {"summarize": {"model": "two"}}},
        ],
        catalog,
    )
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
    layer = {
        "policy": {"no_egress": True},
        "stages": {"ask": {"connection": "cloud", "model": "model"}},
    }
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
        assert len(list_connections(session)) == 6
        assert len(list_models(session)) == 6
        forced = next(
            model for model in list_models(session) if model["connection_key"] == "align:whisperx"
        )
        assert forced["model_key"] == "wav2vec2-auto"
        assert forced["capabilities"]["metadata"]["forced_alignment"] is True
        # Bootstrap catalogs no concrete Codex/OpenAI text model.
        assert all(
            connection["provider_type"] != "codex-local"
            for connection in list_connections(session)
        )
        profiles = list_profiles(session)
        assert len(profiles) == 1
        assert set(profiles[0]["stages"]) == {stage.value for stage in ProviderStage}
        assert profiles[0]["stages"]["correct"]["connection"] == "correct:ollama"
        assert profiles[0]["stages"]["correct"]["model"] == Settings().llm.ollama.model
        assert all(connection["secret_ref"] is None for connection in list_connections(session))
        session.add(PlaudFile(id="recording", filename="test"))
        session.flush()
        selected = select_recording_override(
            session, "recording", first_id, stages={"ask": {"options": {"x": 1}}}
        )
        assert selected["profile_id"] == first_id
        resolved = resolve_recording_profile(session, "recording").to_dict()
        assert resolved["stages"]["ask"]["options"] == {"x": 1}
        assert resolved["layers"][-1] == "recording:recording"


def test_bootstrap_requires_an_explicit_openai_text_model(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'openai-model.db'}")
    Base.metadata.create_all(engine)
    settings = Settings()
    settings.llm.provider = "openai"
    with Session(engine) as session, pytest.raises(ValueError, match="model is not set"):
        bootstrap_default_profile(session, settings)
    settings.llm.openai.model = "explicit-model"
    with Session(engine) as session:
        profile = bootstrap_default_profile(session, settings)
        selection = next(item for item in profile.stage_selections if item.stage == "summarize")
        assert session.get(ModelCatalogEntry, selection.model_id).model_key == "explicit-model"


def test_recording_embed_profile_change_durably_requeues_transcript_index(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'profile-reindex.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        bootstrap_default_profile(session, Settings())
        session.add(PlaudFile(id="recording", filename="Meeting"))
        session.flush()
        original = resolve_recording_profile(session, "recording").to_dict()
        session.add_all(
            [
                Chunk(
                    file_id="recording",
                    idx=0,
                    text="indexed transcript",
                    dim=1,
                    embedding=b"\x00\x00\x80?",
                    resolved_profile_snapshot=original,
                ),
                StageRun(
                    file_id="recording",
                    stage=StageName.index,
                    status=StageStatus.completed,
                    detail={},
                ),
            ]
        )
        stages = dict(list_profiles(session)[0]["stages"])
        stages["embed"] = dict(stages["embed"]) | {"options": {"space": "v2"}}
        changed = create_profile_version(
            session,
            {
                "key": "changed-embed",
                "name": "Changed embed",
                "stages": stages,
                "privacy_policy": "allow-egress",
                "no_egress": False,
                "fallback_policy": {},
            },
        )
        select_recording_override(session, "recording", changed["id"])
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "recording", StageRun.stage == StageName.index
            )
        )
        assert session.query(Chunk).filter_by(file_id="recording").count() == 0
        assert run.status == StageStatus.pending
        assert run.detail["reindex_only"] is True


def test_recording_profile_change_rechecks_active_claim_under_lock(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'profile-busy.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        profile = bootstrap_default_profile(session, Settings())
        session.add(
            PlaudFile(
                id="busy-recording",
                processing_token="active-worker",
                processing_lease_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        session.commit()
        profile_id = profile.id

    with Session(engine) as session:
        with pytest.raises(ProfileMutationBusyError, match="processing"):
            select_recording_override(session, "busy-recording", profile_id)
        session.rollback()
        assert session.get(RecordingProfileOverride, "busy-recording") is None


def test_postgresql_profile_resolution_uses_matching_shared_library_fence():
    statements: list[str] = []

    class Bind:
        class dialect:
            name = "postgresql"

    class FakeSession:
        def get_bind(self):
            return Bind()

        def execute(self, statement):
            statements.append(str(statement))

    lock_library_profile_resolution(FakeSession())

    assert statements == ["SELECT pg_advisory_xact_lock_shared(1280330574)"]


def test_provider_mutation_rejects_active_dispatch_but_recovers_expired_lease(
    tmp_path,
):
    from localplaud.db.models import ProviderCostReservation
    from localplaud.providers.service import lock_recording_profile_change

    engine = create_engine(f"sqlite:///{tmp_path / 'dispatch-busy.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        bootstrap_default_profile(session, Settings())
        session.add(PlaudFile(id="dispatch-recording"))
        session.add(
            ProviderCostReservation(
                id="active-dispatch",
                scope_key="file:dispatch-recording",
                file_id="dispatch-recording",
                operation="ask",
                status="active",
                owner="web-process",
                lease_until=datetime.now(UTC) + timedelta(minutes=5),
                profile_fingerprint="f" * 64,
            )
        )
        session.commit()

    with Session(engine) as session:
        with pytest.raises(ProfileMutationBusyError, match="provider request"):
            lock_recording_profile_change(session, "dispatch-recording")
        session.rollback()
        reservation = session.get(ProviderCostReservation, "active-dispatch")
        reservation.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    with Session(engine) as session:
        assert lock_recording_profile_change(session, "dispatch-recording") is not None


def test_removed_codex_text_provider_keeps_history_but_cannot_be_selected(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'codex-retired.db'}")
    Base.metadata.create_all(engine)
    text_capability = _cap(
        ProviderStage.correct,
        ProviderStage.summarize,
        ProviderStage.mind_map,
        ProviderStage.ask,
        egress=True,
    ).model_dump(mode="json")
    with Session(engine) as session:
        bootstrap_default_profile(session, Settings())
        # A deployed database keeps the historical connection, model and profile.
        connection = ProviderConnection(
            key="correct:codex-local",
            name="Codex CLI (experimental)",
            provider_type="codex-local",
            execution_target="cloud",
            data_egress=True,
        )
        session.add(connection)
        session.flush()
        model = ModelCatalogEntry(
            connection_id=connection.id,
            model_key="gpt-6.1-sol",
            display_name="Codex gpt-6.1-sol (experimental)",
            capabilities=text_capability,
        )
        session.add(model)
        session.flush()
        old = ExecutionProfile(key="old-codex", name="Old Codex", version=1)
        old.stage_selections.append(
            ProfileStageSelection(
                stage="summarize", connection_id=connection.id, model_id=model.id, options={}
            )
        )
        session.add(old)
        session.flush()

        listed = next(item for item in list_profiles(session) if item["key"] == "old-codex")
        assert listed["stages"]["summarize"]["connection"] == "correct:codex-local"
        assert any(item["model_key"] == "gpt-6.1-sol" for item in list_models(session))
        # Editing the historical rows (for example disabling them) stays possible.
        save_connection(session, {"name": "Codex CLI (removed)"}, connection.id)
        save_model(session, {"enabled": False}, model.id)

        with pytest.raises(ValueError, match="codex-local text provider was removed"):
            save_connection(
                session,
                {
                    "key": "custom:codex",
                    "name": "Custom Codex",
                    "provider_type": "codex-local",
                    "execution_target": "cloud",
                    "data_egress": True,
                    "config": {},
                },
            )
        with pytest.raises(ValueError, match="codex-local text provider was removed"):
            save_model(
                session,
                {
                    "connection_id": connection.id,
                    "model_key": "gpt-new",
                    "display_name": "gpt-new",
                    "capabilities": text_capability,
                },
            )
        with pytest.raises(ValueError, match="codex-local text provider was removed"):
            create_profile_version(
                session,
                {
                    "key": "new-codex",
                    "name": "New Codex",
                    "stages": {
                        "summarize": {"connection": "correct:codex-local", "model": "gpt-6.1-sol"}
                    },
                },
            )
        default = next(item for item in list_profiles(session) if item["is_system_default"])
        with pytest.raises(ValueError, match="codex-local text provider was removed"):
            create_profile_version(
                session,
                {
                    "key": "codex-fallback",
                    "name": "Codex fallback",
                    "stages": {
                        stage: {"connection": item["connection"], "model": item["model"]}
                        for stage, item in default["stages"].items()
                    },
                    "fallback_policy": {
                        "stages": {
                            "summarize": [
                                {"connection": "correct:codex-local", "model": "gpt-6.1-sol"}
                            ]
                        }
                    },
                },
            )


def test_historical_codex_snapshot_fails_visibly_at_dispatch():
    from localplaud.llm.base import LLMUnavailable, build_llm

    snapshot = {
        "stages": {
            "summarize": {
                "connection": "correct:codex-local",
                "provider_type": "codex-local",
                "model": "gpt-6.1-sol",
                "configuration": {"polish_chunk_chars": 8000, "timeout_seconds": 1800},
            }
        }
    }
    selected = _settings_for_stage(Settings(), snapshot, "summarize")
    with pytest.raises(LLMUnavailable, match="select the ai-gateway"):
        build_llm(selected.llm)


def test_runtime_projection_rejects_gateway_non_text_snapshot():
    snapshot = {
        "stages": {
            "embed": {
                "connection": "llm:ai-gateway",
                "provider_type": "ai-gateway",
                "model": "gpt-test",
            }
        }
    }
    with pytest.raises(ValueError, match="supports only.*embed"):
        _settings_for_stage(Settings(), snapshot, "embed")


@pytest.mark.parametrize("invalid_budget", [0, 1_000_000, "bad"])
def test_runtime_projection_revalidates_provider_chunk_budget(invalid_budget):
    snapshot = {
        "stages": {
            "correct": {
                "connection": "llm:ai-gateway",
                "provider_type": "ai-gateway",
                "model": "sky-quality",
                "configuration": {"polish_chunk_chars": invalid_budget},
            }
        }
    }
    with pytest.raises(ValueError):
        _settings_for_stage(Settings(), snapshot, "correct")


def test_recording_resolution_layers_provenance_and_reference_guards(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'resolution-layers.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        system = bootstrap_default_profile(session, Settings())
        profiles = {
            key: ExecutionProfile(key=key, name=key.title(), version=1)
            for key in ("folder", "rule", "template", "manual", "rule-action")
        }
        session.add_all(profiles.values())
        session.flush()
        folder = Folder(name="Meetings", execution_profile_id=profiles["folder"].id)
        session.add(folder)
        session.flush()
        recording = PlaudFile(id="layered", filename="Layered", folder_id=folder.id)
        session.add(recording)
        session.add(
            NoteTemplate(
                key="layered",
                version=1,
                name="Layered",
                system_prompt="system",
                instructions="instructions",
                execution_profile_id=profiles["template"].id,
                is_active=True,
            )
        )
        rule = AutomationRule(
            name="Profile rule",
            priority=10,
            actions={"profile_id": profiles["rule-action"].id},
        )
        session.add(rule)
        session.flush()
        session.add(
            RecordingRuleProfileAssignment(
                file_id=recording.id,
                profile_id=profiles["rule"].id,
                rule_id=rule.id,
                rule_version=3,
                priority_snapshot=10,
                rule_snapshot={"name": rule.name},
            )
        )
        winning_rule = AutomationRule(name="Tie winner", priority=10, actions={})
        session.add(winning_rule)
        session.flush()
        session.add(
            RecordingRuleProfileAssignment(
                file_id=recording.id,
                profile_id=profiles["rule"].id,
                rule_id=winning_rule.id,
                rule_version=4,
                priority_snapshot=10,
                rule_snapshot={"name": winning_rule.name},
            )
        )
        session.add(
            RecordingProfileOverride(
                file_id=recording.id,
                profile_id=profiles["manual"].id,
                stage_overrides={},
                policy_overrides={},
            )
        )
        session.flush()

        resolved = resolve_recording_profile(
            session, recording.id, template_key="layered"
        ).to_dict()
        assert resolved["schema"] == "localplaud-resolved-profile/v2"
        assert [item["kind"] for item in resolved["layer_provenance"]] == [
            "system",
            "folder",
            "rule",
            "template",
            "recording_profile",
            "recording_patch",
        ]
        assert {
            key: resolved["layer_provenance"][2][key]
            for key in ("source_rule_id", "rule_version", "priority")
        } == {
            "source_rule_id": winning_rule.id,
            "rule_version": 4,
            "priority": 10,
        }
        assert resolved["layer_provenance"][-2]["profile_id"] == profiles["manual"].id
        with pytest.raises(ValueError, match="recording"):
            delete_profile(session, profiles["manual"].id)

        clear_recording_override(session, recording.id)
        inherited = resolve_recording_profile(
            session, recording.id, template_key="layered"
        ).to_dict()
        assert inherited["layer_provenance"][-1]["kind"] == "template"
        assert system.id == inherited["layer_provenance"][0]["profile_id"]

        for key, message in (
            ("folder", "folder"),
            ("rule", "AutoFlow"),
            ("template", "note template"),
            ("rule-action", "AutoFlow rule"),
        ):
            with pytest.raises(ValueError, match=message):
                delete_profile(session, profiles[key].id)


def test_legacy_provider_profile_schema_rebuild_preserves_ids_and_config(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-providers.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.execute(
            text("""
            CREATE TABLE provider_connections (
                id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL UNIQUE,
                provider_type VARCHAR(64) NOT NULL, base_url VARCHAR(1024),
                secret_ref VARCHAR(256), configuration JSON NOT NULL,
                enabled BOOLEAN NOT NULL, version INTEGER NOT NULL,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            )
        """)
        )
        connection.execute(
            text("""
            CREATE TABLE model_catalog_entries (
                id INTEGER PRIMARY KEY, connection_id INTEGER NOT NULL,
                model_key VARCHAR(256) NOT NULL, display_name VARCHAR(256) NOT NULL,
                capabilities JSON NOT NULL, enabled BOOLEAN NOT NULL,
                FOREIGN KEY(connection_id) REFERENCES provider_connections(id)
            )
        """)
        )
        connection.execute(
            text("""
            CREATE TABLE execution_profiles (
                id INTEGER PRIMARY KEY, name VARCHAR(128) NOT NULL UNIQUE,
                description TEXT, stages JSON NOT NULL, policy JSON NOT NULL,
                is_system_default BOOLEAN NOT NULL, enabled BOOLEAN NOT NULL,
                version INTEGER NOT NULL, created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """)
        )
        connection.execute(
            text("""
            CREATE TABLE profile_stage_selections (
                id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL,
                stage VARCHAR(32) NOT NULL, connection_id INTEGER NOT NULL,
                model_id INTEGER NOT NULL, options JSON NOT NULL,
                FOREIGN KEY(profile_id) REFERENCES execution_profiles(id),
                FOREIGN KEY(connection_id) REFERENCES provider_connections(id),
                FOREIGN KEY(model_id) REFERENCES model_catalog_entries(id)
            )
        """)
        )
        connection.execute(
            text("""
            INSERT INTO provider_connections VALUES (
                7, 'openai-cloud', 'openai', 'https://api.openai.com/v1',
                'env:OPENAI_API_KEY', '{"timeout": 45}', 1, 2,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
        """)
        )
        connection.execute(
            text("""
            INSERT INTO model_catalog_entries VALUES (
                11, 7, 'gpt-test', 'GPT Test', '{}', 1
            )
        """)
        )
        connection.execute(
            text("""
            INSERT INTO execution_profiles VALUES (
                3, 'system-default', NULL, '{}',
                '{"no_egress": false, "cost_ceiling": 2.5}',
                1, 1, 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
        """)
        )
        connection.execute(
            text("""
            INSERT INTO profile_stage_selections VALUES (19, 3, 'ask', 7, 11, '{}')
        """)
        )

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
        assert {item.stage for item in upgraded.stage_selections} == {
            stage.value for stage in ProviderStage
        }
        correct = next(item for item in upgraded.stage_selections if item.stage == "correct")
        assert session.get(ProviderConnection, correct.connection_id).provider_type == "ollama"
        assert bootstrap_default_profile(session, Settings()).id == upgraded.id
        selection = session.get(ProfileStageSelection, 19)
        assert (selection.profile_id, selection.connection_id, selection.model_id) == (3, 7, 11)


def test_partial_default_profile_reuses_deployed_connections_and_fills_all_stages(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'partial-default.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for key, provider in (
            ("mlx-whisper", "mlx-whisper"),
            ("pyannote", "pyannote"),
            ("ollama", "ollama"),
        ):
            session.add(
                ProviderConnection(
                    key=key,
                    name=key,
                    provider_type=provider,
                    execution_target="local",
                    data_egress=False,
                )
            )
        opencode = ProviderConnection(
            key="correct:opencode-go",
            name="OpenCode Go",
            provider_type="opencode-go",
            execution_target="cloud",
            data_egress=True,
        )
        session.add(opencode)
        session.flush()
        model = ModelCatalogEntry(
            connection_id=opencode.id,
            model_key="qwen3.7-plus",
            display_name="qwen3.7-plus",
            capabilities={},
        )
        session.add(model)
        session.flush()
        partial = ExecutionProfile(
            key="legacy-settings-default",
            name="partial",
            version=3,
            is_system_default=True,
        )
        partial.stage_selections.append(
            ProfileStageSelection(
                stage="correct",
                connection_id=opencode.id,
                model_id=model.id,
                options={},
            )
        )
        session.add(partial)
        session.commit()

        upgraded = bootstrap_default_profile(
            session,
            Settings(
                asr={"provider": "mlx-whisper"},
                diarize={"provider": "pyannote"},
                llm={"provider": "ollama"},
                embeddings={"provider": "ollama"},
            ),
        )
        session.commit()
        assert upgraded.version == 4
        assert {item.stage for item in upgraded.stage_selections} == {
            stage.value for stage in ProviderStage
        }
        assert session.query(ProviderConnection).count() == 5
        assert {item.connection.key for item in upgraded.stage_selections} == {
            "mlx-whisper",
            "pyannote",
            "ollama",
        }
        assert (
            bootstrap_default_profile(session, Settings(asr={"provider": "mlx-whisper"})).id
            == upgraded.id
        )


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


def test_correct_dispatch_uses_durable_connection_identity_config_and_secret(monkeypatch):
    settings = Settings()
    original_api_key = settings.llm.openai.api_key
    monkeypatch.setenv("CORRECTION_OPENAI_KEY", "stage-key")
    snapshot = {
        "policy": {"no_egress": False},
        "stages": {
            "correct": {
                "connection": "correction:primary",
                "provider_type": "openai",
                "model": "gpt-correction",
                "configuration": {"base_url": "https://llm.example.test/v1"},
                "secret_ref": "env:CORRECTION_OPENAI_KEY",
                "options": {},
            }
        },
    }

    resolved = _settings_for_stage(settings, snapshot, "correct")

    assert resolved.llm.provider == "openai"
    assert resolved.llm.openai.model == "gpt-correction"
    assert resolved.llm.openai.base_url == "https://llm.example.test/v1"
    assert resolved.llm.openai.api_key == "stage-key"
    assert settings.llm.provider == "ollama"
    assert settings.llm.openai.api_key == original_api_key


def test_resolved_correct_snapshot_preserves_connection_dispatch_data(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'correct-profile.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        bootstrap_default_profile(
            session,
            Settings(
                llm={
                    "provider": "openai",
                    "openai": {
                        "model": "gpt-correction",
                        "base_url": "https://llm.example.test/v1",
                    },
                }
            ),
        )
        session.commit()

        resolved = preview_resolution(session).to_dict()["stages"]["correct"]

        assert resolved == {
            "connection": "correct:openai",
            "model": "gpt-correction",
            "options": {},
            "provider_type": "openai",
            # api_mode and polish_chunk_chars are dispatch data too: which API
            # surface the endpoint is driven through, and how much transcript
            # one correction request may carry.
            "configuration": {
                "base_url": "https://llm.example.test/v1",
                "api_mode": "chat",
                "polish_chunk_chars": 12000,
            },
            "secret_ref": None,
            "execution_target": "cloud",
            "data_egress": True,
        }


def test_connection_config_rejects_nested_credentials_and_filters_legacy_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'provider-secrets.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="config.headers.Authorization"):
            save_connection(
                session,
                {
                    "key": "correct:unsafe",
                    "name": "Unsafe",
                    "provider_type": "openai",
                    "config": {"headers": {"Authorization": "Bearer raw-secret"}},
                },
            )

        profile = bootstrap_default_profile(session, Settings())
        correct = next(item for item in profile.stage_selections if item.stage == "correct")
        connection = session.get(ProviderConnection, correct.connection_id)
        connection.config = {
            "host": "http://localhost:11434",
            "nested": {"access_token": "legacy-secret", "timeout": 30},
        }
        session.commit()

        snapshot = preview_resolution(session).to_dict()["stages"]["correct"]
        assert snapshot["configuration"] == {
            "host": "http://localhost:11434",
            "nested": {"timeout": 30},
        }
        assert connection.config["nested"]["access_token"] == "legacy-secret"


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


def test_legacy_summary_schema_rebuild_preserves_data_and_is_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-summary.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO plaud_files (id) VALUES ('r1')"))
        connection.execute(
            text("""
            CREATE TABLE summaries (
                id INTEGER PRIMARY KEY,
                file_id VARCHAR(64) NOT NULL,
                template VARCHAR(64) NOT NULL,
                title VARCHAR(512),
                content_md TEXT NOT NULL,
                llm_provider VARCHAR(64),
                model VARCHAR(128),
                source VARCHAR(16) NOT NULL,
                revision INTEGER NOT NULL,
                transcript_revision INTEGER,
                profile_snapshot JSON NOT NULL,
                created_at DATETIME NOT NULL
            )
        """)
        )
        connection.execute(
            text("""
            INSERT INTO summaries (
                id, file_id, template, content_md, source, revision,
                transcript_revision, profile_snapshot, created_at
            ) VALUES (1, 'r1', 'default', '# Notes', 'local', 3, 2,
                      '{"version": 1}', CURRENT_TIMESTAMP)
        """)
        )

    assert migrate_legacy_summary_schema(engine) == ["summaries"]
    assert migrate_legacy_summary_schema(engine) == []
    columns = {column["name"] for column in inspect(engine).get_columns("summaries")}
    assert not {"revision", "transcript_revision", "profile_snapshot"} & columns
    with engine.connect() as connection:
        row = connection.execute(
            text("""
            SELECT content_md, input_transcript_revision, resolved_profile_snapshot
            FROM summaries WHERE id = 1
        """)
        ).one()
    assert row.content_md == "# Notes"
    assert row.input_transcript_revision == 2
    assert '"version": 1' in row.resolved_profile_snapshot


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
        assert '<h1 class="page sf-large-title">Settings</h1>' in settings_page.text
        assert 'aria-label="Settings sections"' in settings_page.text


def test_settings_resolution_preview_explains_scope_layers(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    config.get_settings(reload=True)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    with TestClient(app) as client:
        with db_session.session_scope() as session:
            folder_profile = ExecutionProfile(
                key="folder-local", name="Folder local", version=2, no_egress=True
            )
            session.add(folder_profile)
            session.flush()
            folder = Folder(name="Clients", execution_profile_id=folder_profile.id)
            session.add(folder)
            session.add(PlaudFile(id="preview-rec", filename="Preview"))
            session.flush()
            folder_id = folder.id

        system_only = client.get("/api/providers/resolution-preview")
        assert system_only.status_code == 200
        resolved = system_only.json()["resolved"]
        assert [item["kind"] for item in resolved["layer_provenance"]] == ["system"]
        assert "data_egress" in resolved["stages"]["transcribe"]

        scoped = client.get(f"/api/providers/resolution-preview?folder_id={folder_id}")
        assert scoped.status_code == 200
        provenance = scoped.json()["resolved"]["layer_provenance"]
        assert [item["kind"] for item in provenance] == ["system", "folder"]
        assert provenance[1]["folder_name"] == "Clients"
        assert provenance[1]["profile_key"] == "folder-local"
        assert scoped.json()["resolved"]["policy"]["no_egress"] is True

        recording = client.get("/api/providers/resolution-preview?file_id=preview-rec")
        assert recording.status_code == 200
        assert recording.json()["resolved"]["layers"][0].startswith("system:")

        assert client.get("/api/providers/resolution-preview?folder_id=9999").status_code == 404
        assert client.get("/api/providers/resolution-preview?file_id=missing").status_code == 404
        missing_template = client.get("/api/providers/resolution-preview?template_key=nope")
        assert missing_template.status_code == 404

        page = client.get("/settings")
        assert page.status_code == 200
        assert 'id="resolution-preview-form"' in page.text
        assert 'id="resolution-preview-result" class="resolution-preview-result" aria-live="polite"' in page.text
        assert f'<option value="{folder_id}">Clients</option>' in page.text
        assert "/api/providers/resolution-preview?" in page.text


def test_profile_resolution_schema_migration_is_additive_and_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'profile-resolution-schema.db'}")
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE folders (id INTEGER PRIMARY KEY)",
            "CREATE TABLE note_templates (id INTEGER PRIMARY KEY)",
            "CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)",
            "CREATE TABLE execution_profiles (id INTEGER PRIMARY KEY)",
            "CREATE TABLE automation_runs (id INTEGER PRIMARY KEY)",
        ):
            connection.execute(text(statement))
        connection.execute(text("INSERT INTO folders (id) VALUES (7)"))
        connection.execute(text("INSERT INTO note_templates (id) VALUES (9)"))
    assert set(migrate_profile_resolution_schema(engine)) == {
        "folders.execution_profile_id",
        "note_templates.execution_profile_id",
        "recording_rule_profile_assignments",
    }
    assert migrate_profile_resolution_schema(engine) == []
    inspector = inspect(engine)
    assignment_columns = {
        column["name"] for column in inspector.get_columns("recording_rule_profile_assignments")
    }
    assert {
        "file_id",
        "profile_id",
        "rule_id",
        "rule_version",
        "priority_snapshot",
        "automation_run_id",
        "rule_snapshot",
    } <= assignment_columns
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT id FROM folders")) == 7
        assert connection.scalar(text("SELECT id FROM note_templates")) == 9
    assert {
        foreign_key["referred_table"]
        for foreign_key in inspector.get_foreign_keys("recording_rule_profile_assignments")
    } == {"plaud_files", "execution_profiles", "automation_runs"}


def test_profile_resolution_migration_skips_sqlite_pragma_on_postgresql(monkeypatch):
    from contextlib import contextmanager

    import localplaud.db.migrations as migrations

    class Inspector:
        def get_table_names(self):
            return ["folders", "note_templates", "recording_rule_profile_assignments"]

        def get_columns(self, _table):
            return [{"name": "execution_profile_id"}]

    class Connection:
        def exec_driver_sql(self, _statement):
            raise AssertionError("PostgreSQL migration must not execute SQLite PRAGMA")

    class Engine:
        class dialect:
            name = "postgresql"

        @contextmanager
        def begin(self):
            yield Connection()

    monkeypatch.setattr(migrations, "inspect", lambda _bind: Inspector())
    assert migrations.migrate_profile_resolution_schema(Engine()) == []


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

        rejected_codex = client.post(
            "/api/providers/connections",
            json={
                "key": "correct:test-codex",
                "name": "Test Codex",
                "provider_type": "codex-local",
                "execution_target": "cloud",
                "data_egress": True,
                "config": {},
            },
        )
        assert rejected_codex.status_code == 422
        assert "codex-local text provider was removed" in rejected_codex.json()["detail"]

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


def test_remote_worker_connection_health_uses_handshake_and_checks_model(monkeypatch, tmp_path):
    from localplaud.remote.protocol import HandshakeResponse, StageCapability

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'remote-health.db'}"
    )
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
        health = client.post(f"/api/providers/connections/{connection['id']}/health").json()
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
        available_health = client.post(f"/api/providers/models/{available['id']}/health").json()
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
        missing_health = client.post(f"/api/providers/models/{missing['id']}/health").json()
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
        session.add(
            StageRun(file_id="r", stage="transcribe", resolved_profile_snapshot={"version": 1})
        )
        session.commit()
        assert session.query(StageRun).one().resolved_profile_snapshot == {"version": 1}


def test_cloud_and_remote_starting_profiles_are_explicit_opt_in(monkeypatch, tmp_path):
    from localplaud.db.models import RemoteWorker

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    config.get_settings(reload=True)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    with TestClient(app) as client:
        default_before = next(
            item for item in client.get("/api/providers/profiles").json()["profiles"]
            if item["is_system_default"]
        )
        catalog = client.get("/api/providers/starting-profiles").json()["starting_profiles"]
        assert [item["kind"] for item in catalog] == [
            "openai-cloud", "openai-compatible", "remote-gpu"
        ]
        assert all("leav" in item["egress"] or "upload" in item["egress"] or "send" in item["egress"]
                   for item in catalog)

        url = "/api/providers/starting-profiles"
        # Egress must be acknowledged; raw keys are not secret references.
        assert client.post(f"{url}/openai-cloud", json={}).status_code == 422
        bad_secret = client.post(
            f"{url}/openai-cloud",
            json={"acknowledge_egress": True, "secret_env": "sk-live-raw-key"},
        )
        assert bad_secret.status_code == 422
        assert client.post(
            f"{url}/openai-cloud", json={"acknowledge_egress": True, "api_key": "x"}
        ).status_code == 422
        assert client.post(f"{url}/nope", json={"acknowledge_egress": True}).status_code == 404

        cloud = client.post(
            f"{url}/openai-cloud",
            json={"acknowledge_egress": True, "secret_env": "OPENAI_API_KEY", "model": "gpt-x"},
        )
        assert cloud.status_code == 201, cloud.text
        profile = cloud.json()
        assert profile["is_system_default"] is False
        assert profile["policy"]["no_egress"] is False
        for stage in ("correct", "summarize", "mind_map", "ask"):
            assert profile["stages"][stage]["connection"] == "llm:openai-cloud"
            assert profile["stages"][stage]["model"] == "gpt-x"
        for stage in ("transcribe", "diarize", "embed"):
            assert profile["stages"][stage] == default_before["stages"][stage]
        connection = next(
            item for item in client.get("/api/providers/connections").json()["connections"]
            if item["key"] == "llm:openai-cloud"
        )
        assert connection["secret_ref"] == "env:OPENAI_API_KEY"
        assert connection["data_egress"] is True

        compatible_missing = client.post(
            f"{url}/openai-compatible",
            json={"acknowledge_egress": True, "secret_env": "LLM_KEY", "model": "m"},
        )
        assert compatible_missing.status_code == 422
        compatible = client.post(
            f"{url}/openai-compatible",
            json={
                "acknowledge_egress": True,
                "secret_env": "LLM_KEY",
                "base_url": "https://llm.example/v1/",
                "model": "qwen",
            },
        )
        assert compatible.status_code == 201, compatible.text
        assert compatible.json()["stages"]["summarize"]["connection"] == "llm:compatible:llm.example-v1"

        no_worker = client.post(
            f"{url}/remote-gpu", json={"acknowledge_egress": True, "worker_key": "gpu"}
        )
        assert no_worker.status_code == 404
        created = client.post(
            "/api/providers/workers",
            json={"key": "gpu", "name": "GPU", "base_url": "https://gpu.example/"},
        )
        assert created.status_code == 201
        untested = client.post(
            f"{url}/remote-gpu", json={"acknowledge_egress": True, "worker_key": "gpu"}
        )
        assert untested.status_code == 422
        with db_session.session_scope() as session:
            worker = session.scalar(select(RemoteWorker).where(RemoteWorker.key == "gpu"))
            worker.capabilities = [
                {"stage": "transcribe", "models": ["turbo"], "metadata": {}},
                {"stage": "diarize", "models": ["pyannote"], "metadata": {}},
            ]
            connection = session.scalar(
                select(ProviderConnection).where(ProviderConnection.key == "worker:gpu")
            )
            for model_key, stage in (("turbo", "transcribe"), ("pyannote", "diarize")):
                session.add(
                    ModelCatalogEntry(
                        connection_id=connection.id,
                        model_key=model_key,
                        display_name=model_key,
                        capabilities=Capability(
                            execution_target="remote_worker",
                            data_egress=True,
                            stages=(StageCapabilities(stage=ProviderStage(stage)),),
                        ).model_dump(mode="json"),
                    )
                )
        remote = client.post(
            f"{url}/remote-gpu", json={"acknowledge_egress": True, "worker_key": "gpu"}
        )
        assert remote.status_code == 201, remote.text
        assert remote.json()["stages"]["transcribe"]["connection"] == "worker:gpu"
        assert remote.json()["stages"]["diarize"]["model"] == "pyannote"
        assert remote.json()["is_system_default"] is False

        default_after = next(
            item for item in client.get("/api/providers/profiles").json()["profiles"]
            if item["is_system_default"]
        )
        assert default_after["id"] == default_before["id"]

        page = client.get("/settings")
        assert page.status_code == 200
        assert 'class="starter-form" data-kind="openai-cloud"' in page.text
        assert 'name="acknowledge_egress" required' in page.text
        assert "Only the variable name is stored" in page.text
