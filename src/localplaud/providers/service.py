"""Headless CRUD/read surface and legacy Settings bootstrap."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session, selectinload

from ..config import Settings, get_settings
from ..db.models import (
    AskThread,
    AutomationRule,
    ExecutionProfile,
    Folder,
    KnowledgeDocument,
    ModelCatalogEntry,
    NoteTemplate,
    PlaudFile,
    ProfileStageSelection,
    ProviderConnection,
    ProviderCostReservation,
    RecordingProfileOverride,
    RecordingRuleProfileAssignment,
    RemoteWorker,
)
from .contracts import Capability, Health, ProviderStage, StageCapabilities
from .resolver import ResolvedProfile, resolve_profile
from .usage import lock_cost_budget

DEFAULT_PROFILE_KEY = "legacy-settings-default"
_STAGE_FAMILY = {
    "transcribe": "asr",
    "align": "asr",
    "diarize": "diarize",
    "correct": "correct",
    "summarize": "llm",
    "mind_map": "llm",
    "embed": "embeddings",
    "ask": "llm",
}
_CREDENTIAL_CONFIG_KEYS = {
    "api_key",
    "authorization",
    "bearer",
    "cookie",
    "credential",
    "hf_token",
    "password",
    "secret",
    "token",
}


class ProfileMutationBusyError(RuntimeError):
    pass


def _is_cloud(name: str) -> bool:
    return name in {
        "openai",
        "deepgram",
        "assemblyai",
        "anthropic",
        "opencode-go",
        "codex-local",
    }


def _credential_config_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return normalized in _CREDENTIAL_CONFIG_KEYS or normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )


def _validate_connection_config(value: Any, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _credential_config_key(key):
                raise ValueError(
                    f"raw credentials are not accepted in {path}.{key}; use secret_ref"
                )
            _validate_connection_config(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _validate_connection_config(item, f"{path}[{index}]")


def _snapshot_connection_config(value: Any) -> Any:
    """Exclude credential-shaped legacy fields without mutating deployed rows."""
    if isinstance(value, dict):
        return {
            key: _snapshot_connection_config(item)
            for key, item in value.items()
            if not _credential_config_key(key)
        }
    if isinstance(value, list):
        return [_snapshot_connection_config(item) for item in value]
    return value


def _model_for(settings: Settings, family: str, provider: str) -> str:
    config = getattr(getattr(settings, family), provider.replace("-", "_"))
    return str(getattr(config, "model", provider))


def _capability(stages: list[ProviderStage], *, cloud: bool) -> dict:
    return Capability(
        execution_target="cloud" if cloud else "local",
        data_egress=cloud,
        health=Health(),
        stages=tuple(
            StageCapabilities(
                stage=stage,
                timestamps="word"
                if stage in {ProviderStage.transcribe, ProviderStage.align}
                else "none",
                speaker_output=stage == ProviderStage.diarize,
                hardware_requirement=None if cloud else "configured local runtime",
            )
            for stage in stages
        ),
    ).model_dump(mode="json")


def _settings_specs(settings: Settings):
    if settings.llm.provider == "codex-local":
        raise ValueError(
            "codex-local is correction-only and cannot bootstrap a general LLM connection"
        )
    return [
        (
            "asr",
            settings.asr.provider,
            _model_for(settings, "asr", settings.asr.provider),
            [ProviderStage.transcribe, ProviderStage.align],
        ),
        ("diarize", settings.diarize.provider, settings.diarize.model, [ProviderStage.diarize]),
        (
            "correct",
            settings.llm.provider,
            _model_for(settings, "llm", settings.llm.provider),
            [ProviderStage.correct],
        ),
        (
            "llm",
            settings.llm.provider,
            _model_for(settings, "llm", settings.llm.provider),
            [ProviderStage.summarize, ProviderStage.mind_map, ProviderStage.ask],
        ),
        (
            "embeddings",
            settings.embeddings.provider,
            _model_for(settings, "embeddings", settings.embeddings.provider),
            [ProviderStage.embed],
        ),
    ]


def _settings_connection_config(settings: Settings, family: str, provider: str) -> dict:
    """Snapshot non-secret Settings fields needed to dispatch this connection."""
    config_family = "llm" if family == "correct" else family
    provider_config = getattr(getattr(settings, config_family), provider.replace("-", "_"), None)
    if provider_config is None:
        return {}
    values = provider_config.model_dump(mode="json", exclude={"api_key", "hf_token"})
    values.pop("model", None)
    if values.get("reasoning_effort") is None:
        values.pop("reasoning_effort", None)
    return values


def _with_required_capabilities(raw: dict, stages: list[ProviderStage], *, cloud: bool) -> dict:
    """Repair old Settings catalog rows while preserving valid custom metadata."""
    try:
        capability = Capability.model_validate(raw)
    except ValueError:
        return _capability(stages, cloud=cloud)
    existing = {item.stage for item in capability.stages}
    missing = [stage for stage in stages if stage not in existing]
    if not missing:
        return raw
    generated = Capability.model_validate(_capability(missing, cloud=cloud))
    return capability.model_copy(
        update={"stages": capability.stages + generated.stages}
    ).model_dump(mode="json")


def _ensure_settings_entries(
    session: Session, settings: Settings
) -> dict[str, tuple[ProviderConnection, ModelCatalogEntry]]:
    """Return catalog entries for every Settings-backed stage family.

    Deployed databases predate the family-prefixed connection keys used by a
    clean install. Reuse their compatible provider connection instead of
    duplicating it, while always adding the explicit model/capability row that
    immutable profile selections require.
    """
    entries: dict[str, tuple[ProviderConnection, ModelCatalogEntry]] = {}
    for family, provider, model_key, stages in _settings_specs(settings):
        preferred_keys = (f"{family}:{provider}", provider)
        connection = session.scalar(
            select(ProviderConnection)
            .where(
                ProviderConnection.provider_type == provider,
                ProviderConnection.key.in_(preferred_keys),
            )
            .order_by(ProviderConnection.id)
        )
        if connection is None:
            connection = ProviderConnection(
                key=f"{family}:{provider}",
                name=f"{provider} ({family})",
                provider_type=provider,
                execution_target="cloud" if _is_cloud(provider) else "local",
                data_egress=_is_cloud(provider),
                secret_ref=None,
                config=_settings_connection_config(settings, family, provider),
            )
            session.add(connection)
            session.flush()
        entry = session.scalar(
            select(ModelCatalogEntry).where(
                ModelCatalogEntry.connection_id == connection.id,
                ModelCatalogEntry.model_key == model_key,
            )
        )
        if entry is None:
            entry = ModelCatalogEntry(
                connection_id=connection.id,
                model_key=model_key,
                display_name=model_key,
                capabilities=_capability(stages, cloud=_is_cloud(provider)),
            )
            session.add(entry)
            session.flush()
        else:
            entry.capabilities = _with_required_capabilities(
                entry.capabilities, stages, cloud=_is_cloud(provider)
            )
        entries[family] = (connection, entry)
    return entries


def _ensure_forced_alignment_entry(
    session: Session,
) -> tuple[ProviderConnection, ModelCatalogEntry]:
    """Catalog the optional local WhisperX aligner without selecting it by default."""
    connection = session.scalar(
        select(ProviderConnection).where(ProviderConnection.key == "align:whisperx")
    )
    if connection is None:
        connection = ProviderConnection(
            key="align:whisperx",
            name="WhisperX forced alignment",
            provider_type="whisperx",
            execution_target="local",
            data_egress=False,
            config={"device": "auto", "interpolate_method": "nearest"},
            health={"status": "unknown", "detail": "optional runtime not checked"},
        )
        session.add(connection)
        session.flush()
    model = session.scalar(
        select(ModelCatalogEntry).where(
            ModelCatalogEntry.connection_id == connection.id,
            ModelCatalogEntry.model_key == "wav2vec2-auto",
        )
    )
    if model is None:
        model = ModelCatalogEntry(
            connection_id=connection.id,
            model_key="wav2vec2-auto",
            display_name="WhisperX language-specific wav2vec2 (auto)",
            capabilities=Capability(
                execution_target="local",
                data_egress=False,
                health=Health(status="unknown", detail="optional runtime not checked"),
                stages=(
                    StageCapabilities(
                        stage=ProviderStage.align,
                        languages=("en", "zh"),
                        timestamps="word",
                        hardware_requirement="CUDA recommended; CPU supported",
                    ),
                ),
                metadata={
                    "forced_alignment": True,
                    "implementation": "whisperx-wav2vec2",
                    "model_selection": "language-specific",
                },
            ).model_dump(mode="json"),
        )
        session.add(model)
        session.flush()
    return connection, model


def _ensure_codex_entry(
    session: Session, settings: Settings
) -> tuple[ProviderConnection, ModelCatalogEntry]:
    """Catalog the opt-in Codex CLI correction path without selecting it."""
    config = settings.llm.codex_local
    connection = session.scalar(
        select(ProviderConnection).where(ProviderConnection.key == "correct:codex-local")
    )
    snapshot = config.model_dump(mode="json", exclude={"model"})
    if connection is None:
        connection = ProviderConnection(
            key="correct:codex-local",
            name="Codex CLI (experimental)",
            provider_type="codex-local",
            execution_target="cloud",
            data_egress=True,
            secret_ref=None,
            config=snapshot,
            health={
                "status": "unknown",
                "detail": "requires an explicit trusted-single-user ChatGPT login",
            },
        )
        session.add(connection)
        session.flush()
    model = session.scalar(
        select(ModelCatalogEntry).where(
            ModelCatalogEntry.connection_id == connection.id,
            ModelCatalogEntry.model_key == config.model,
        )
    )
    if model is None:
        model = ModelCatalogEntry(
            connection_id=connection.id,
            model_key=config.model,
            display_name=f"Codex {config.model} (experimental)",
            capabilities=Capability(
                execution_target="cloud",
                data_egress=True,
                health=Health(
                    status="unknown",
                    detail="requires an explicit trusted-single-user ChatGPT login",
                ),
                stages=(
                    StageCapabilities(
                        stage=ProviderStage.correct,
                        hardware_requirement="local Codex CLI; cloud inference",
                    ),
                ),
                metadata={
                    "experimental": True,
                    "trusted_single_user_only": True,
                    "auth_owner": "codex-cli",
                    "billing": "Codex entitlement or configured Codex authentication",
                },
            ).model_dump(mode="json"),
        )
        session.add(model)
        session.flush()
    return connection, model


def _profile_is_complete(session: Session, profile: ExecutionProfile) -> bool:
    by_stage = {selection.stage: selection for selection in profile.stage_selections}
    if set(by_stage) != set(_STAGE_FAMILY):
        return False
    for selection in by_stage.values():
        model = session.get(ModelCatalogEntry, selection.model_id)
        if model is None or model.connection_id != selection.connection_id:
            return False
    correct = session.get(ModelCatalogEntry, by_stage["correct"].model_id)
    try:
        capability = Capability.model_validate(correct.capabilities)
    except ValueError:
        return False
    if capability.for_stage(ProviderStage.correct) is None:
        return False
    return True


def bootstrap_default_profile(session: Session, settings: Settings) -> ExecutionProfile:
    """Create a Settings-equivalent profile once, without changing runtime dispatch."""
    _ensure_forced_alignment_entry(session)
    _ensure_codex_entry(session, settings)
    existing = session.scalar(
        select(ExecutionProfile)
        .where(ExecutionProfile.key == DEFAULT_PROFILE_KEY)
        .order_by(ExecutionProfile.version.desc())
        .options(selectinload(ExecutionProfile.stage_selections))
    )
    if existing is not None:
        if _profile_is_complete(session, existing):
            return existing
        return _upgrade_default_profile(session, existing, settings)

    entries = _ensure_settings_entries(session, settings)

    cloud = any(connection.data_egress for connection, _ in entries.values())
    profile = ExecutionProfile(
        key=DEFAULT_PROFILE_KEY,
        name="Current Settings",
        version=1,
        is_system_default=True,
        privacy_policy="allow-egress" if cloud else "local-only",
        no_egress=not cloud,
        fallback_policy={"stages": {}},
    )
    session.add(profile)
    session.flush()
    for stage, family in _STAGE_FAMILY.items():
        connection, entry = entries[family]
        profile.stage_selections.append(
            ProfileStageSelection(
                stage=stage,
                connection_id=connection.id,
                model_id=entry.id,
                options={},
            )
        )
    session.flush()
    return profile


def _upgrade_default_profile(
    session: Session, previous: ExecutionProfile, settings: Settings
) -> ExecutionProfile:
    """Create a complete immutable default version from a partial legacy one."""
    entries = _ensure_settings_entries(session, settings)

    for row in session.scalars(
        select(ExecutionProfile).where(ExecutionProfile.is_system_default.is_(True))
    ):
        row.is_system_default = False
    upgraded = ExecutionProfile(
        key=previous.key,
        name="Current Settings",
        version=previous.version + 1,
        is_system_default=True,
        privacy_policy=previous.privacy_policy,
        no_egress=previous.no_egress,
        cost_ceiling=previous.cost_ceiling,
        fallback_policy=previous.fallback_policy,
    )
    session.add(upgraded)
    session.flush()
    previous_by_stage = {
        selection.stage: selection
        for selection in previous.stage_selections
        if selection.stage in _STAGE_FAMILY and selection.stage != "correct"
    }
    for stage, family in _STAGE_FAMILY.items():
        selection = previous_by_stage.get(stage)
        if selection is None:
            connection, model = entries[family]
            selection = ProfileStageSelection(
                stage=stage,
                connection_id=connection.id,
                model_id=model.id,
                options={},
            )
        else:
            selection = ProfileStageSelection(
                stage=stage,
                connection_id=selection.connection_id,
                model_id=selection.model_id,
                options=selection.options,
            )
        upgraded.stage_selections.append(selection)
    session.flush()
    return upgraded


def list_connections(session: Session) -> list[dict[str, Any]]:
    return [
        {
            "id": r.id,
            "key": r.key,
            "name": r.name,
            "provider_type": r.provider_type,
            "execution_target": r.execution_target,
            "data_egress": r.data_egress,
            "secret_ref": r.secret_ref,
            "config": r.config,
            "health": r.health,
        }
        for r in session.scalars(select(ProviderConnection).order_by(ProviderConnection.id))
    ]


def list_models(session: Session) -> list[dict[str, Any]]:
    return [
        {
            "id": r.id,
            "connection_id": r.connection_id,
            "connection_key": session.get(ProviderConnection, r.connection_id).key,
            "model_key": r.model_key,
            "display_name": r.display_name,
            "capabilities": r.capabilities,
            "enabled": r.enabled,
        }
        for r in session.scalars(select(ModelCatalogEntry).order_by(ModelCatalogEntry.id))
    ]


def _profile_layer(profile: ExecutionProfile) -> dict[str, Any]:
    return {
        "key": profile.key,
        "policy": {
            "privacy_policy": profile.privacy_policy,
            "no_egress": profile.no_egress,
            "cost_ceiling": profile.cost_ceiling,
            "fallback_policy": profile.fallback_policy,
        },
        "stages": {
            row.stage: {
                "connection": row.connection.key,
                "model": row.model_entry.model_key,
                "options": row.options,
            }
            for row in profile.stage_selections
        },
    }


def _profile_query():
    return select(ExecutionProfile).options(
        selectinload(ExecutionProfile.stage_selections).selectinload(
            ProfileStageSelection.connection
        ),
        selectinload(ExecutionProfile.stage_selections).selectinload(
            ProfileStageSelection.model_entry
        ),
    )


def _capability_catalog(session: Session) -> dict[tuple[str, str], dict]:
    catalog: dict[tuple[str, str], dict] = {}
    disabled_workers = {
        f"worker:{worker.key}"
        for worker in session.scalars(select(RemoteWorker).where(RemoteWorker.enabled.is_(False)))
    }
    for model in session.scalars(select(ModelCatalogEntry)):
        connection = session.get(ProviderConnection, model.connection_id)
        if (
            connection is not None
            and connection.key not in disabled_workers
            and model.enabled
        ):
            catalog[(connection.key, model.model_key)] = model.capabilities
    return catalog


def _connection_catalog(session: Session) -> dict[str, dict[str, Any]]:
    """Runtime connection identity persisted into each resolved snapshot."""
    disabled_workers = {
        f"worker:{worker.key}"
        for worker in session.scalars(select(RemoteWorker).where(RemoteWorker.enabled.is_(False)))
    }
    return {
        connection.key: {
            "provider_type": connection.provider_type,
            "execution_target": connection.execution_target,
            "data_egress": connection.data_egress,
            "configuration": _snapshot_connection_config(connection.config or {}),
            "secret_ref": connection.secret_ref,
        }
        for connection in session.scalars(select(ProviderConnection))
        if connection.key not in disabled_workers
    }


def list_profiles(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(_profile_query().order_by(ExecutionProfile.id))
    return [
        {
            "id": row.id,
            "name": row.name,
            "version": row.version,
            "is_system_default": row.is_system_default,
            **_profile_layer(row),
        }
        for row in rows
    ]


def preview_resolution(session: Session, *partial_layers: dict | None) -> ResolvedProfile:
    profile = session.scalar(
        _profile_query()
        .where(ExecutionProfile.is_system_default)
        .order_by(ExecutionProfile.version.desc(), ExecutionProfile.id.desc())
    )
    if profile is None:
        raise ValueError("no system default execution profile")
    return resolve_profile(
        [_profile_layer(profile), *partial_layers],
        _capability_catalog(session),
        _connection_catalog(session),
    )


def _profile_resolution_layer(
    session: Session,
    profile_id: int | None,
    label: str,
    *,
    kind: str,
    source: dict[str, Any] | None = None,
) -> dict | None:
    if profile_id is None:
        return None
    profile = session.scalar(_profile_query().where(ExecutionProfile.id == profile_id))
    if profile is None:
        raise ValueError(f"{label} profile {profile_id} no longer exists")
    layer = _profile_layer(profile)
    layer["key"] = f"{label}:{profile.key}@{profile.version}"
    layer["provenance"] = {
        "kind": kind,
        "profile_id": profile.id,
        "profile_key": profile.key,
        "profile_version": profile.version,
        **(source or {}),
    }
    return layer


def resolve_recording_profile(
    session: Session, file_id: str, *, template_key: str | None = None
) -> ResolvedProfile:
    """Resolve system -> folder -> rule -> template -> recording layers."""
    system = session.scalar(
        _profile_query()
        .where(ExecutionProfile.is_system_default)
        .order_by(ExecutionProfile.version.desc(), ExecutionProfile.id.desc())
    )
    if system is None:
        raise ValueError("no system default execution profile")
    recording = session.get(PlaudFile, file_id)
    if recording is None:
        raise ValueError(f"recording {file_id} no longer exists")

    system_layer = _profile_layer(system)
    system_layer["key"] = f"system:{system.key}@{system.version}"
    system_layer["provenance"] = {
        "kind": "system",
        "profile_id": system.id,
        "profile_key": system.key,
        "profile_version": system.version,
    }
    layers: list[dict | None] = [system_layer]

    folder = session.get(Folder, recording.folder_id) if recording.folder_id else None
    if folder is not None:
        layers.append(
            _profile_resolution_layer(
                session,
                folder.execution_profile_id,
                f"folder:{folder.id}",
                kind="folder",
                source={"folder_id": folder.id, "folder_name": folder.name},
            )
        )

    rule_assignment = session.scalar(
        select(RecordingRuleProfileAssignment)
        .where(RecordingRuleProfileAssignment.file_id == file_id)
        .order_by(
            RecordingRuleProfileAssignment.priority_snapshot,
            RecordingRuleProfileAssignment.rule_id.desc(),
        )
    )
    if rule_assignment is not None:
        label = f"rule:{rule_assignment.rule_id}@{rule_assignment.rule_version}"
        layers.append(
            _profile_resolution_layer(
                session,
                rule_assignment.profile_id,
                label,
                kind="rule",
                source={
                    "source_rule_id": rule_assignment.rule_id,
                    "rule_version": rule_assignment.rule_version,
                    "priority": rule_assignment.priority_snapshot,
                    "automation_run_id": rule_assignment.automation_run_id,
                    "rule": rule_assignment.rule_snapshot or {},
                },
            )
        )

    selected_template_key = template_key
    if selected_template_key is None:
        selected_template_key = (
            recording.note_template_key or get_settings().pipeline.summary_template
        )
    if selected_template_key != "auto":
        template = session.scalar(
            select(NoteTemplate)
            .where(
                NoteTemplate.key == selected_template_key,
                NoteTemplate.is_active.is_(True),
            )
            .order_by(NoteTemplate.version.desc())
        )
        if template is not None:
            layers.append(
                _profile_resolution_layer(
                    session,
                    template.execution_profile_id,
                    f"template:{template.key}@{template.version}",
                    kind="template",
                    source={
                        "template_key": template.key,
                        "template_version": template.version,
                    },
                )
            )

    override = session.get(RecordingProfileOverride, file_id)
    if override is not None:
        layers.append(
            _profile_resolution_layer(
                session,
                override.profile_id,
                f"recording-profile:{file_id}",
                kind="recording_profile",
                source={"file_id": file_id},
            )
        )
        layers.append(
            {
                "key": f"recording:{file_id}",
                "stages": override.stage_overrides,
                "policy": override.policy_overrides,
                "provenance": {"kind": "recording_patch", "file_id": file_id},
            }
        )
    return resolve_profile(layers, _capability_catalog(session), _connection_catalog(session))


def lock_recording_profile_change(session: Session, file_id: str) -> PlaudFile | None:
    from ..worker.pipeline import processing_claim_active

    lock_cost_budget(session, file_id)
    recording = session.get(PlaudFile, file_id)
    if recording is None:
        return None
    session.refresh(recording)
    if processing_claim_active(recording):
        raise ProfileMutationBusyError(
            "recording is processing; change profile when it finishes"
        )
    _reject_active_provider_operations(session, file_id=file_id)
    return recording


def lock_library_profile_resolution(session: Session) -> None:
    """Share the library fence before a PostgreSQL recording/profile lock.

    Global profile and membership mutations take the matching exclusive lock.
    SQLite serializes these paths through its database write lock instead.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock_shared(1280330574)"))


def lock_library_profile_membership_change(session: Session) -> None:
    """Fence profile-affecting membership changes before set enumeration."""
    lock_cost_budget(session, None)


def lock_recording_membership_changes(session: Session, file_ids: list[str]) -> None:
    """Fence known profile-affecting memberships and lock rows in order."""
    lock_library_profile_membership_change(session)
    lock_recording_profile_changes(session, file_ids)


def _reject_active_provider_operations(
    session: Session,
    *,
    file_id: str | None,
) -> None:
    """Reject mutation while the same profile scope has committed to dispatch."""
    now = datetime.now(UTC)
    ask_filter = (
        or_(AskThread.file_id == file_id, AskThread.file_id.is_(None))
        if file_id is not None
        else AskThread.file_id.is_(None)
    )
    if session.scalar(
        select(AskThread.id)
        .where(
            ask_filter,
            AskThread.request_token.is_not(None),
            AskThread.request_lease_until > now,
        )
        .limit(1)
    ) is not None:
        raise ProfileMutationBusyError(
            "Ask is using this provider scope; change the profile when it finishes"
        )
    document_filter = (
        KnowledgeDocument.file_id == file_id
        if file_id is not None
        else KnowledgeDocument.file_id.is_(None)
    )
    if session.scalar(
        select(KnowledgeDocument.id)
        .where(
            document_filter,
            KnowledgeDocument.status == "running",
            KnowledgeDocument.lease_token.is_not(None),
            KnowledgeDocument.lease_until > now,
        )
        .limit(1)
    ) is not None:
        raise ProfileMutationBusyError(
            "note indexing is using this provider scope; change the profile when it finishes"
        )
    scope_key = f"file:{file_id}" if file_id is not None else "library"
    if session.scalar(
        select(ProviderCostReservation.id)
        .where(
            ProviderCostReservation.scope_key == scope_key,
            ProviderCostReservation.status == "active",
            ProviderCostReservation.lease_until > now,
        )
        .limit(1)
    ) is not None:
        raise ProfileMutationBusyError(
            "a provider request is active; change the profile when it finishes"
        )


def lock_library_profile_change(session: Session) -> list[str]:
    """Fence a provider/profile mutation against every recording worker."""
    lock_library_profile_membership_change(session)
    file_ids = list(session.scalars(select(PlaudFile.id).order_by(PlaudFile.id)))
    for file_id in file_ids:
        lock_recording_profile_change(session, file_id)
    _reject_active_provider_operations(session, file_id=None)
    return file_ids


def lock_recording_profile_changes(session: Session, file_ids: list[str]) -> None:
    for file_id in sorted(set(file_ids)):
        lock_recording_profile_change(session, file_id)


def select_recording_override(
    session: Session,
    file_id: str,
    profile_id: int,
    *,
    stages: dict | None = None,
    policy: dict | None = None,
) -> dict:
    if (
        lock_recording_profile_change(session, file_id) is None
        or session.get(ExecutionProfile, profile_id) is None
    ):
        raise LookupError("recording or profile not found")
    row = session.get(RecordingProfileOverride, file_id)
    if row is None:
        row = RecordingProfileOverride(file_id=file_id, profile_id=profile_id)
        session.add(row)
    row.profile_id = profile_id
    row.stage_overrides = stages or {}
    row.policy_overrides = policy or {}
    session.flush()
    from ..worker.knowledge_index import sync_file_knowledge_documents

    sync_file_knowledge_documents(session, file_id)
    return {
        "file_id": file_id,
        "profile_id": profile_id,
        "stages": row.stage_overrides,
        "policy": row.policy_overrides,
    }


def clear_recording_override(session: Session, file_id: str) -> dict:
    if lock_recording_profile_change(session, file_id) is None:
        raise LookupError("recording not found")
    row = session.get(RecordingProfileOverride, file_id)
    if row is not None:
        session.delete(row)
        session.flush()
    from ..worker.knowledge_index import sync_file_knowledge_documents

    sync_file_knowledge_documents(session, file_id)
    return {"file_id": file_id, "profile_id": None}


def select_folder_profile(session: Session, folder_id: int, profile_id: int | None) -> dict:
    folder = session.get(Folder, folder_id)
    if folder is None:
        raise LookupError("folder not found")
    if profile_id is not None and session.get(ExecutionProfile, profile_id) is None:
        raise LookupError("profile not found")
    # The affected set is dynamic. Fence new claims/membership changes before
    # enumeration, then lock recording rows in deterministic order.
    lock_library_profile_membership_change(session)
    file_ids = list(
        session.scalars(
            select(PlaudFile.id)
            .where(PlaudFile.folder_id == folder_id)
            .order_by(PlaudFile.id)
        )
    )
    lock_recording_profile_changes(session, file_ids)
    folder.execution_profile_id = profile_id
    session.flush()
    from ..worker.knowledge_index import sync_file_knowledge_documents

    for file_id in file_ids:
        sync_file_knowledge_documents(session, file_id)
    return {"folder_id": folder.id, "profile_id": folder.execution_profile_id}


def save_connection(session: Session, data: dict, connection_id: int | None = None) -> dict:
    """Create or update a provider connection without accepting raw credentials."""
    if any(key in data for key in ("api_key", "token", "password", "secret")):
        raise ValueError("raw credentials are not accepted; use secret_ref")
    _validate_connection_config(data.get("config", {}))
    row = session.get(ProviderConnection, connection_id) if connection_id else None
    existing = row is not None
    if connection_id and row is None:
        raise LookupError("provider connection not found")
    if existing:
        lock_library_profile_change(session)
    provider_type = data.get("provider_type", getattr(row, "provider_type", None))
    execution_target = data.get("execution_target", getattr(row, "execution_target", None))
    data_egress = data.get("data_egress", getattr(row, "data_egress", None))
    if execution_target == "remote_worker":
        from ..remote.client import validate_provider_timeout

        config = data.get("config", getattr(row, "config", {})) or {}
        validate_provider_timeout(config.get("timeout", 120), field="timeout")
        validate_provider_timeout(config.get("job_timeout", 3600), field="job_timeout")
    if provider_type == "codex-local" and (
        execution_target != "cloud" or data_egress is not True
    ):
        raise ValueError("codex-local requires cloud execution with data egress")
    if row is None:
        row = ProviderConnection(
            key=data["key"], name=data["name"], provider_type=data["provider_type"]
        )
        session.add(row)
    for field in (
        "key",
        "name",
        "provider_type",
        "execution_target",
        "data_egress",
        "secret_ref",
        "config",
    ):
        if field in data:
            setattr(row, field, data[field])
    session.flush()
    if existing:
        from ..worker.knowledge_index import sync_knowledge_documents

        sync_knowledge_documents(session)
    return (
        list_connections(session)[-1]
        if connection_id is None
        else next(item for item in list_connections(session) if item["id"] == row.id)
    )


def delete_connection(session: Session, connection_id: int) -> None:
    row = session.get(ProviderConnection, connection_id)
    if row is None:
        raise LookupError("provider connection not found")
    lock_library_profile_change(session)
    if session.scalar(
        select(ModelCatalogEntry.id).where(ModelCatalogEntry.connection_id == connection_id)
    ):
        raise ValueError("connection still has models")
    session.delete(row)


def _secret_value(secret_ref: str | None) -> str | None:
    if not secret_ref:
        return None
    if not secret_ref.startswith("env:"):
        raise ValueError("unsupported secret reference; expected env:VARIABLE")
    return os.environ.get(secret_ref.removeprefix("env:"))


def _probe_connection(row: ProviderConnection, model_key: str | None = None) -> tuple[bool, str]:
    """Run the selected provider's real model-aware health implementation."""
    if row.execution_target == "remote_worker":
        from ..remote.client import RemoteWorkerClient

        config = dict(row.config or {})
        if "token_env" not in config and (row.secret_ref or "").startswith("env:"):
            config["token_env"] = row.secret_ref.removeprefix("env:")
        if not config.get("base_url"):
            raise ValueError("remote worker base_url is not configured")
        client = RemoteWorkerClient.from_config(config)
        try:
            handshake = client.handshake()
        finally:
            client.close()
        advertised: dict[str, set[str]] = {}
        for capability in handshake.capabilities:
            for advertised_model in capability.models:
                advertised.setdefault(advertised_model, set()).add(capability.stage.value)
        if model_key is not None and model_key not in advertised:
            return False, f"model {model_key} is not advertised by worker {handshake.worker_id}"
        stages = (
            sorted(advertised[model_key])
            if model_key is not None
            else sorted({item.stage.value for item in handshake.capabilities})
        )
        detail = (
            f"worker {handshake.worker_id} · protocol {handshake.version} · "
            f"{len(advertised)} model(s) · stages {', '.join(stages) or 'none'}"
        )
        return True, detail
    family = row.key.split(":", 1)[0]
    settings = get_settings().model_copy(deep=True)
    secret = _secret_value(row.secret_ref)

    if family == "align":
        from ..worker.align import health

        options = dict(row.config or {})
        return health(row.provider_type, model_key, options)
    if family == "asr":
        from ..asr.registry import build_provider

        settings.asr.provider = row.provider_type
        cfg = getattr(settings.asr, row.provider_type.replace("-", "_"))
        for key, value in (row.config or {}).items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        if model_key and hasattr(cfg, "model"):
            cfg.model = model_key
        if secret and hasattr(cfg, "api_key"):
            cfg.api_key = secret
        provider = build_provider(row.provider_type, settings.asr)
    elif family in {"llm", "correct"}:
        from ..llm.base import build_llm

        settings.llm.provider = row.provider_type
        cfg = getattr(settings.llm, row.provider_type.replace("-", "_"))
        for key, value in (row.config or {}).items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        if model_key and hasattr(cfg, "model"):
            cfg.model = model_key
        if secret and hasattr(cfg, "api_key"):
            cfg.api_key = secret
        provider = build_llm(settings.llm)
    elif family == "embeddings":
        from ..embeddings.base import build_embedder

        settings.embeddings.provider = row.provider_type
        cfg = getattr(settings.embeddings, row.provider_type.replace("-", "_"))
        for key, value in (row.config or {}).items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        if model_key and hasattr(cfg, "model"):
            cfg.model = model_key
        if secret and hasattr(cfg, "api_key"):
            cfg.api_key = secret
        provider = build_embedder(settings.embeddings)
    elif family == "diarize":
        from ..worker.diarize import health

        settings.diarize.provider = row.provider_type
        if model_key:
            settings.diarize.model = model_key
        if secret:
            settings.diarize.hf_token = secret
        return health(settings.diarize)
    else:
        return False, f"unsupported provider family: {family}"

    health = getattr(provider, "health", None)
    if callable(health):
        result = health()
        return result if isinstance(result, tuple) else (bool(result), "health check completed")
    return bool(provider.available()), "provider availability check"


def check_connection_health(session: Session, connection_id: int) -> dict:
    row = session.get(ProviderConnection, connection_id)
    if row is None:
        raise LookupError("provider connection not found")
    try:
        ok, detail = _probe_connection(row)
        status = "healthy" if ok else "degraded"
    except Exception as exc:  # noqa: BLE001 - health must return structured degradation
        status, detail = "unavailable", str(exc)
    row.health = {
        "status": status,
        "detail": detail,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    session.flush()
    return row.health


def check_model_health(session: Session, model_id: int) -> dict:
    model = session.get(ModelCatalogEntry, model_id)
    if model is None:
        raise LookupError("model not found")
    connection = session.get(ProviderConnection, model.connection_id)
    if connection is None:
        raise LookupError("provider connection not found")
    try:
        ok, detail = _probe_connection(connection, model.model_key)
        status = "healthy" if ok else "degraded"
    except Exception as exc:  # noqa: BLE001
        status, detail = "unavailable", str(exc)
    capability = dict(model.capabilities or {})
    capability["health"] = {
        "status": status,
        "detail": detail,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    model.capabilities = capability
    session.flush()
    return capability["health"]


def save_model(session: Session, data: dict, model_id: int | None = None) -> dict:
    row = session.get(ModelCatalogEntry, model_id) if model_id else None
    existing = row is not None
    if model_id and row is None:
        raise LookupError("model not found")
    if existing:
        lock_library_profile_change(session)
    connection = session.get(
        ProviderConnection, data.get("connection_id", getattr(row, "connection_id", None))
    )
    if connection is None:
        raise LookupError("provider connection not found")
    capabilities = data.get("capabilities", getattr(row, "capabilities", {}))
    if connection.provider_type == "codex-local":
        codex_capability = Capability.model_validate(capabilities)
        if (
            codex_capability.execution_target != "cloud"
            or not codex_capability.data_egress
        ):
            raise ValueError("codex-local requires cloud execution with data egress")
        invalid_stages = [
            item.stage.value
            for item in codex_capability.stages
            if item.stage != ProviderStage.correct
        ]
        if invalid_stages:
            raise ValueError(
                "codex-local is correction-only; unsupported stages: "
                + ", ".join(invalid_stages)
            )
    if row is None:
        row = ModelCatalogEntry(
            connection_id=data["connection_id"],
            model_key=data["model_key"],
            display_name=data.get("display_name", data["model_key"]),
        )
        session.add(row)
    for field in ("connection_id", "model_key", "display_name", "capabilities", "enabled"):
        if field in data:
            setattr(row, field, data[field])
    session.flush()
    if existing:
        from ..worker.knowledge_index import sync_knowledge_documents

        sync_knowledge_documents(session)
    return next(item for item in list_models(session) if item["id"] == row.id)


def delete_model(session: Session, model_id: int) -> None:
    row = session.get(ModelCatalogEntry, model_id)
    if row is None:
        raise LookupError("model not found")
    lock_library_profile_change(session)
    if session.scalar(
        select(ProfileStageSelection.id).where(ProfileStageSelection.model_id == model_id)
    ):
        raise ValueError("model is used by an execution profile")
    session.delete(row)


def create_profile_version(session: Session, data: dict) -> dict:
    """Create an immutable profile version and its validated stage selections."""
    key = data["key"]
    version = (
        data.get("version")
        or (
            session.scalar(
                select(func.max(ExecutionProfile.version)).where(ExecutionProfile.key == key)
            )
            or 0
        )
        + 1
    )
    if data.get("is_system_default"):
        lock_library_profile_change(session)
        for current in session.scalars(
            select(ExecutionProfile).where(ExecutionProfile.is_system_default)
        ):
            current.is_system_default = False
    row = ExecutionProfile(
        key=key,
        name=data["name"],
        version=version,
        is_system_default=bool(data.get("is_system_default")),
        privacy_policy=data.get("privacy_policy", "allow-egress"),
        no_egress=bool(data.get("no_egress")),
        cost_ceiling=data.get("cost_ceiling"),
        fallback_policy=data.get("fallback_policy", {}),
    )
    session.add(row)
    session.flush()
    for stage, selection in data.get("stages", {}).items():
        connection = session.scalar(
            select(ProviderConnection).where(ProviderConnection.key == selection["connection"])
        )
        if connection is None:
            raise LookupError(f"provider connection not found: {selection['connection']}")
        model = session.scalar(
            select(ModelCatalogEntry).where(
                ModelCatalogEntry.connection_id == connection.id,
                ModelCatalogEntry.model_key == selection["model"],
            )
        )
        if model is None:
            raise LookupError(f"model not found: {selection['model']}")
        row.stage_selections.append(
            ProfileStageSelection(
                stage=stage,
                connection_id=connection.id,
                model_id=model.id,
                options=selection.get("options", {}),
            )
        )
    session.flush()
    # Reuse the resolver as the write-time policy/capability gate.
    resolve_profile(
        [_profile_layer(row)], _capability_catalog(session), _connection_catalog(session)
    )
    if row.is_system_default:
        from ..worker.knowledge_index import sync_knowledge_documents

        sync_knowledge_documents(session)
    return next(item for item in list_profiles(session) if item["id"] == row.id)


def delete_profile(session: Session, profile_id: int) -> None:
    row = session.get(ExecutionProfile, profile_id)
    if row is None:
        raise LookupError("profile not found")
    lock_library_profile_change(session)
    if row.is_system_default:
        raise ValueError("cannot delete the system default profile")
    if session.scalar(
        select(RecordingProfileOverride.file_id).where(
            RecordingProfileOverride.profile_id == profile_id
        )
    ):
        raise ValueError("profile is selected by a recording")
    if session.scalar(select(Folder.id).where(Folder.execution_profile_id == profile_id)):
        raise ValueError("profile is selected by a folder")
    if session.scalar(
        select(NoteTemplate.id).where(NoteTemplate.execution_profile_id == profile_id)
    ):
        raise ValueError("profile is selected by a note template")
    if session.scalar(
        select(RecordingRuleProfileAssignment.file_id).where(
            RecordingRuleProfileAssignment.profile_id == profile_id
        )
    ):
        raise ValueError("profile was selected by an AutoFlow")
    for rule in session.scalars(select(AutomationRule)):
        if (rule.actions or {}).get("profile_id") == profile_id:
            raise ValueError("profile is selected by an AutoFlow rule")
    session.delete(row)


def install_hardware_recommendation(
    session: Session, recommendation_key: str, *, make_default: bool = False
) -> dict:
    """Create an idempotent profile that changes only local ASR/alignment.

    All other stages and the current privacy/cost/fallback policy are cloned from
    the system default. A recommendation cannot be installed until runtime probes
    prove it ready on this host.
    """
    from .hardware import hardware_recommendations

    recommendation = next(
        (
            item
            for item in hardware_recommendations()["recommendations"]
            if item["key"] == recommendation_key
        ),
        None,
    )
    if recommendation is None:
        raise LookupError("hardware recommendation not found")
    if not recommendation["ready"]:
        raise ValueError(recommendation["reason"])

    profile_key = f"recommended-{recommendation_key}"
    existing = session.scalar(
        _profile_query()
        .where(ExecutionProfile.key == profile_key)
        .order_by(ExecutionProfile.version.desc())
    )
    if existing is not None:
        if make_default and not existing.is_system_default:
            lock_library_profile_change(session)
            for current in session.scalars(
                select(ExecutionProfile).where(ExecutionProfile.is_system_default)
            ):
                current.is_system_default = False
            existing.is_system_default = True
            session.flush()
            from ..worker.knowledge_index import sync_knowledge_documents

            sync_knowledge_documents(session)
        return next(item for item in list_profiles(session) if item["id"] == existing.id)

    connection_key = f"asr:{recommendation['provider']}"
    connection = session.scalar(
        select(ProviderConnection).where(ProviderConnection.key == connection_key)
    )
    if connection is None:
        connection = ProviderConnection(
            key=connection_key,
            name=f"{recommendation['provider']} (local ASR)",
            provider_type=recommendation["provider"],
            execution_target="local",
            data_egress=False,
            config={},
            health={"status": "healthy", "detail": recommendation["reason"]},
        )
        session.add(connection)
        session.flush()
    model = session.scalar(
        select(ModelCatalogEntry).where(
            ModelCatalogEntry.connection_id == connection.id,
            ModelCatalogEntry.model_key == recommendation["model"],
        )
    )
    if model is None:
        model = ModelCatalogEntry(
            connection_id=connection.id,
            model_key=recommendation["model"],
            display_name=recommendation["model"],
            capabilities=Capability(
                execution_target="local",
                data_egress=False,
                health=Health(status="healthy", detail=recommendation["reason"]),
                stages=(
                    StageCapabilities(
                        stage=ProviderStage.transcribe,
                        timestamps="word",
                        hardware_requirement=recommendation["hardware"],
                    ),
                    StageCapabilities(
                        stage=ProviderStage.align,
                        timestamps="word",
                        hardware_requirement=recommendation["hardware"],
                    ),
                ),
                metadata={"recommended_by": "local-hardware-v1"},
            ).model_dump(mode="json"),
        )
        session.add(model)
        session.flush()

    current = session.scalar(
        _profile_query()
        .where(ExecutionProfile.is_system_default)
        .order_by(ExecutionProfile.version.desc(), ExecutionProfile.id.desc())
    )
    if current is None:
        raise ValueError("no system default execution profile")
    base = _profile_layer(current)
    stages = dict(base["stages"])
    asr_selection = {
        "connection": connection.key,
        "model": model.model_key,
        "options": recommendation["options"],
    }
    stages[ProviderStage.transcribe.value] = asr_selection
    stages[ProviderStage.align.value] = asr_selection
    policy = base["policy"]
    return create_profile_version(
        session,
        {
            "key": profile_key,
            "name": f"{recommendation['name']} + current stages",
            "is_system_default": make_default,
            "privacy_policy": policy["privacy_policy"],
            "no_egress": policy["no_egress"],
            "cost_ceiling": policy["cost_ceiling"],
            "fallback_policy": policy["fallback_policy"],
            "stages": stages,
        },
    )
