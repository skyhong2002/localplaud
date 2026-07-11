"""Headless CRUD/read surface and legacy Settings bootstrap."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..db.models import (
    ExecutionProfile,
    ModelCatalogEntry,
    PlaudFile,
    ProfileStageSelection,
    ProviderConnection,
    RecordingProfileOverride,
)
from .contracts import Capability, Health, ProviderStage, StageCapabilities
from .resolver import ResolvedProfile, resolve_profile

DEFAULT_PROFILE_KEY = "legacy-settings-default"


def _is_cloud(name: str) -> bool:
    return name in {"openai", "deepgram", "assemblyai", "anthropic"}


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
                timestamps="word" if stage in {ProviderStage.transcribe, ProviderStage.align} else "none",
                speaker_output=stage == ProviderStage.diarize,
                hardware_requirement=None if cloud else "configured local runtime",
            )
            for stage in stages
        ),
    ).model_dump(mode="json")


def bootstrap_default_profile(session: Session, settings: Settings) -> ExecutionProfile:
    """Create a Settings-equivalent profile once, without changing runtime dispatch."""
    existing = session.scalar(
        select(ExecutionProfile)
        .where(ExecutionProfile.key == DEFAULT_PROFILE_KEY, ExecutionProfile.version == 1)
        .options(selectinload(ExecutionProfile.stage_selections))
    )
    if existing is not None:
        return existing

    specs = [
        ("asr", settings.asr.provider, _model_for(settings, "asr", settings.asr.provider),
         [ProviderStage.transcribe, ProviderStage.align]),
        ("diarize", settings.diarize.provider, settings.diarize.model,
         [ProviderStage.diarize]),
        ("llm", settings.llm.provider, _model_for(settings, "llm", settings.llm.provider),
         [ProviderStage.correct, ProviderStage.summarize, ProviderStage.mind_map, ProviderStage.ask]),
        ("embeddings", settings.embeddings.provider,
         _model_for(settings, "embeddings", settings.embeddings.provider), [ProviderStage.embed]),
    ]
    entries: dict[str, tuple[ProviderConnection, ModelCatalogEntry]] = {}
    for family, provider, model, stages in specs:
        connection = ProviderConnection(
            key=f"{family}:{provider}", name=f"{provider} ({family})", provider_type=provider,
            execution_target="cloud" if _is_cloud(provider) else "local",
            data_egress=_is_cloud(provider), secret_ref=None,
        )
        entry = ModelCatalogEntry(
            model_key=model, display_name=model,
            capabilities=_capability(stages, cloud=_is_cloud(provider)),
        )
        session.add(connection)
        session.flush()
        entry.connection_id = connection.id
        session.add(entry)
        session.flush()
        entries[family] = (connection, entry)

    cloud = any(connection.data_egress for connection, _ in entries.values())
    profile = ExecutionProfile(
        key=DEFAULT_PROFILE_KEY, name="Current Settings", version=1, is_system_default=True,
        privacy_policy="allow-egress" if cloud else "local-only", no_egress=not cloud,
        fallback_policy={"asr": list(settings.asr.fallback)},
    )
    session.add(profile)
    session.flush()
    stage_family = {
        "transcribe": "asr", "align": "asr", "diarize": "diarize", "correct": "llm",
        "summarize": "llm", "mind_map": "llm", "embed": "embeddings", "ask": "llm",
    }
    for stage, family in stage_family.items():
        connection, entry = entries[family]
        profile.stage_selections.append(ProfileStageSelection(
            stage=stage, connection_id=connection.id, model_id=entry.id, options={},
        ))
    session.flush()
    return profile


def list_connections(session: Session) -> list[dict[str, Any]]:
    return [{"id": r.id, "key": r.key, "name": r.name, "provider_type": r.provider_type,
             "execution_target": r.execution_target, "data_egress": r.data_egress,
             "secret_ref": r.secret_ref, "health": r.health} for r in session.scalars(
                 select(ProviderConnection).order_by(ProviderConnection.id))]


def list_models(session: Session) -> list[dict[str, Any]]:
    return [{"id": r.id, "connection_id": r.connection_id, "model_key": r.model_key,
             "display_name": r.display_name, "capabilities": r.capabilities,
             "enabled": r.enabled} for r in session.scalars(
                 select(ModelCatalogEntry).order_by(ModelCatalogEntry.id))]


def _profile_layer(profile: ExecutionProfile) -> dict[str, Any]:
    return {"key": profile.key, "policy": {"privacy_policy": profile.privacy_policy,
            "no_egress": profile.no_egress, "cost_ceiling": profile.cost_ceiling,
            "fallback_policy": profile.fallback_policy}, "stages": {
                row.stage: {"connection": row.connection.key, "model": row.model_entry.model_key,
                            "options": row.options} for row in profile.stage_selections}}


def list_profiles(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(select(ExecutionProfile).options(
        selectinload(ExecutionProfile.stage_selections).selectinload(ProfileStageSelection.connection),
        selectinload(ExecutionProfile.stage_selections).selectinload(ProfileStageSelection.model_entry),
    ).order_by(ExecutionProfile.id))
    return [{"id": row.id, "name": row.name, "version": row.version,
             "is_system_default": row.is_system_default, **_profile_layer(row)} for row in rows]


def preview_resolution(session: Session, *partial_layers: dict | None) -> ResolvedProfile:
    profile = session.scalar(
        select(ExecutionProfile)
        .where(ExecutionProfile.is_system_default)
        .order_by(ExecutionProfile.version.desc(), ExecutionProfile.id.desc())
        .options(
            selectinload(ExecutionProfile.stage_selections).selectinload(
                ProfileStageSelection.connection
            ),
            selectinload(ExecutionProfile.stage_selections).selectinload(
                ProfileStageSelection.model_entry
            ),
        )
    )
    if profile is None:
        raise ValueError("no system default execution profile")
    models = session.scalars(select(ModelCatalogEntry))
    catalog = {}
    for model in models:
        connection = session.get(ProviderConnection, model.connection_id)
        catalog[(connection.key, model.model_key)] = model.capabilities
    return resolve_profile([_profile_layer(profile), *partial_layers], catalog)


def select_recording_override(session: Session, file_id: str, profile_id: int,
                              *, stages: dict | None = None, policy: dict | None = None) -> dict:
    if session.get(PlaudFile, file_id) is None or session.get(ExecutionProfile, profile_id) is None:
        raise LookupError("recording or profile not found")
    row = session.get(RecordingProfileOverride, file_id)
    if row is None:
        row = RecordingProfileOverride(file_id=file_id, profile_id=profile_id)
        session.add(row)
    row.profile_id = profile_id
    row.stage_overrides = stages or {}
    row.policy_overrides = policy or {}
    session.flush()
    return {"file_id": file_id, "profile_id": profile_id,
            "stages": row.stage_overrides, "policy": row.policy_overrides}
