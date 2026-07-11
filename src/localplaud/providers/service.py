"""Headless CRUD/read surface and legacy Settings bootstrap."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings, get_settings
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
             "secret_ref": r.secret_ref, "config": r.config, "health": r.health} for r in session.scalars(
                 select(ProviderConnection).order_by(ProviderConnection.id))]


def list_models(session: Session) -> list[dict[str, Any]]:
    return [{"id": r.id, "connection_id": r.connection_id,
             "connection_key": session.get(ProviderConnection, r.connection_id).key,
             "model_key": r.model_key,
             "display_name": r.display_name, "capabilities": r.capabilities,
             "enabled": r.enabled} for r in session.scalars(
                 select(ModelCatalogEntry).order_by(ModelCatalogEntry.id))]


def _profile_layer(profile: ExecutionProfile) -> dict[str, Any]:
    return {"key": profile.key, "policy": {"privacy_policy": profile.privacy_policy,
            "no_egress": profile.no_egress, "cost_ceiling": profile.cost_ceiling,
            "fallback_policy": profile.fallback_policy}, "stages": {
                row.stage: {"connection": row.connection.key, "model": row.model_entry.model_key,
                            "options": row.options} for row in profile.stage_selections}}


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
    for model in session.scalars(select(ModelCatalogEntry)):
        connection = session.get(ProviderConnection, model.connection_id)
        if connection is not None and model.enabled:
            catalog[(connection.key, model.model_key)] = model.capabilities
    return catalog


def list_profiles(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(_profile_query().order_by(ExecutionProfile.id))
    return [{"id": row.id, "name": row.name, "version": row.version,
             "is_system_default": row.is_system_default, **_profile_layer(row)} for row in rows]


def preview_resolution(session: Session, *partial_layers: dict | None) -> ResolvedProfile:
    profile = session.scalar(
        _profile_query()
        .where(ExecutionProfile.is_system_default)
        .order_by(ExecutionProfile.version.desc(), ExecutionProfile.id.desc())
    )
    if profile is None:
        raise ValueError("no system default execution profile")
    return resolve_profile([_profile_layer(profile), *partial_layers], _capability_catalog(session))


def resolve_recording_profile(session: Session, file_id: str) -> ResolvedProfile:
    """Resolve the durable execution profile selected for one recording."""
    system = session.scalar(
        _profile_query()
        .where(ExecutionProfile.is_system_default)
        .order_by(ExecutionProfile.version.desc(), ExecutionProfile.id.desc())
    )
    if system is None:
        raise ValueError("no system default execution profile")

    layers: list[dict | None] = [_profile_layer(system)]
    override = session.get(RecordingProfileOverride, file_id)
    if override is not None:
        selected = session.scalar(
            _profile_query().where(ExecutionProfile.id == override.profile_id)
        )
        if selected is None:
            raise ValueError(f"recording profile {override.profile_id} no longer exists")
        layers.append(_profile_layer(selected))
        layers.append(
            {
                "key": f"recording:{file_id}",
                "stages": override.stage_overrides,
                "policy": override.policy_overrides,
            }
        )
    return resolve_profile(layers, _capability_catalog(session))


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


def save_connection(session: Session, data: dict, connection_id: int | None = None) -> dict:
    """Create or update a provider connection without accepting raw credentials."""
    if any(key in data for key in ("api_key", "token", "password", "secret")):
        raise ValueError("raw credentials are not accepted; use secret_ref")
    row = session.get(ProviderConnection, connection_id) if connection_id else None
    if connection_id and row is None:
        raise LookupError("provider connection not found")
    if row is None:
        row = ProviderConnection(key=data["key"], name=data["name"], provider_type=data["provider_type"])
        session.add(row)
    for field in ("key", "name", "provider_type", "execution_target", "data_egress", "secret_ref", "config"):
        if field in data:
            setattr(row, field, data[field])
    session.flush()
    return list_connections(session)[-1] if connection_id is None else next(
        item for item in list_connections(session) if item["id"] == row.id
    )


def delete_connection(session: Session, connection_id: int) -> None:
    row = session.get(ProviderConnection, connection_id)
    if row is None:
        raise LookupError("provider connection not found")
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
        return False, "remote worker handshake not implemented"
    family = row.key.split(":", 1)[0]
    settings = get_settings().model_copy(deep=True)
    secret = _secret_value(row.secret_ref)

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
    elif family == "llm":
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
    if model_id and row is None:
        raise LookupError("model not found")
    if session.get(ProviderConnection, data.get("connection_id", getattr(row, "connection_id", None))) is None:
        raise LookupError("provider connection not found")
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
    return next(item for item in list_models(session) if item["id"] == row.id)


def delete_model(session: Session, model_id: int) -> None:
    row = session.get(ModelCatalogEntry, model_id)
    if row is None:
        raise LookupError("model not found")
    if session.scalar(
        select(ProfileStageSelection.id).where(ProfileStageSelection.model_id == model_id)
    ):
        raise ValueError("model is used by an execution profile")
    session.delete(row)


def create_profile_version(session: Session, data: dict) -> dict:
    """Create an immutable profile version and its validated stage selections."""
    key = data["key"]
    version = data.get("version") or (
        session.scalar(select(func.max(ExecutionProfile.version)).where(ExecutionProfile.key == key)) or 0
    ) + 1
    if data.get("is_system_default"):
        for current in session.scalars(select(ExecutionProfile).where(ExecutionProfile.is_system_default)):
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
    resolve_profile([_profile_layer(row)], _capability_catalog(session))
    return next(item for item in list_profiles(session) if item["id"] == row.id)


def delete_profile(session: Session, profile_id: int) -> None:
    row = session.get(ExecutionProfile, profile_id)
    if row is None:
        raise LookupError("profile not found")
    if row.is_system_default:
        raise ValueError("cannot delete the system default profile")
    if session.scalar(
        select(RecordingProfileOverride.file_id).where(RecordingProfileOverride.profile_id == profile_id)
    ):
        raise ValueError("profile is selected by a recording")
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
            for current in session.scalars(
                select(ExecutionProfile).where(ExecutionProfile.is_system_default)
            ):
                current.is_system_default = False
            existing.is_system_default = True
            session.flush()
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
