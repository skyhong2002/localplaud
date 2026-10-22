"""Import the legacy config-time providers into durable profile records."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import ExecutionProfile, ProviderConnection, ProviderModel

STAGE_CAPABILITY = {
    "transcribe": "asr", "align": "alignment", "diarize": "diarization",
    "correct": "correction", "summarize": "notes", "mind_map": "mind_map",
    "ask": "ask", "index": "embeddings",
}

LOCAL_PROVIDERS = {"ffmpeg", "mlx-whisper", "faster-whisper", "whispercpp",
                   "pyannote", "ollama", "local", "local-rules",
                   "whisper-timestamps", "whisperx", "none"}
SUPPORTED_PROVIDERS = LOCAL_PROVIDERS | {
    "openai", "deepgram", "assemblyai", "anthropic", "codex-local", "remote-worker",
}

_DEFAULT_SECRET_REFS = {
    "openai": "env:OPENAI_API_KEY",
    "deepgram": "env:LOCALPLAUD_ASR__DEEPGRAM__API_KEY",
    "assemblyai": "env:LOCALPLAUD_ASR__ASSEMBLYAI__API_KEY",
    "anthropic": "env:LOCALPLAUD_LLM__ANTHROPIC__API_KEY",
    "remote-worker": "env:LOCALPLAUD_REMOTE_WORKER_TOKEN",
}
_DEFAULT_BASE_URLS = {"openai": "https://api.openai.com/v1"}


def _egress(provider: str) -> str:
    return "local_only" if provider in LOCAL_PROVIDERS else "external"


def _model_for(settings: Settings, capability: str, provider: str) -> str | None:
    if capability == "asr":
        cfg = getattr(settings.asr, provider.replace("-", "_"))
        return getattr(cfg, "model", None)
    if capability == "diarization":
        return settings.diarize.model
    if capability in {"notes", "mind_map", "ask", "correction"}:
        return getattr(getattr(settings.llm, provider.replace("-", "_")), "model", None)
    if capability == "embeddings":
        return getattr(getattr(settings.embeddings, provider), "model", None)
    return None


def legacy_profile_spec(settings: Settings) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    asr = settings.asr.provider
    llm = settings.llm.provider
    embeddings = settings.embeddings.provider
    diarize = settings.diarize.provider
    stages = {
        "convert": {"provider": "ffmpeg", "egress": "local_only"},
        "transcribe": {"provider": asr, "model": _model_for(settings, "asr", asr),
                       "egress": _egress(asr), "fallback": [
                           {"provider": item, "model": _model_for(settings, "asr", item),
                            "egress": _egress(item)} for item in settings.asr.fallback
                       ], "options": {"language": settings.asr.language}},
        "align": {"provider": settings.align.provider,
                  "model": settings.align.model or settings.align.provider,
                  "egress": "local_only", "options": {"device": settings.align.device,
                                                         "word_timestamps": True}},
        "diarize": {"provider": diarize, "model": settings.diarize.model,
                    "egress": _egress(diarize)},
        "correct": {"provider": "local-rules", "model": "vocabulary-v1",
                    "egress": "local_only"},
        "summarize": {"provider": llm, "model": _model_for(settings, "notes", llm),
                      "egress": _egress(llm)},
        "mind_map": {"provider": llm, "model": _model_for(settings, "mind_map", llm),
                     "egress": _egress(llm)},
        "ask": {"provider": llm, "model": _model_for(settings, "ask", llm),
                "egress": _egress(llm)},
        "index": {"provider": embeddings,
                  "model": _model_for(settings, "embeddings", embeddings),
                  "egress": _egress(embeddings)},
    }
    all_routes = [route for route in stages.values()]
    all_routes += [fallback for route in stages.values() for fallback in route.get("fallback", [])]
    policy = {
        # The default routes still exactly mirror config. This broad system
        # boundary merely permits an explicitly selected cloud/worker starter;
        # local starter profiles tighten themselves back to no-egress.
        "no_egress": False,
        "allowed_providers": sorted(SUPPORTED_PROVIDERS),
        "fallback_requires_explicit_configuration": True,
        "system_boundary_version": 1,
    }
    return stages, policy


def bootstrap_legacy_profile(session: Session, settings: Settings) -> ExecutionProfile:
    """Create an equivalent default once; never overwrite later user edits."""
    existing = session.scalar(select(ExecutionProfile).where(ExecutionProfile.is_system_default))
    if existing is not None:
        if existing.version == 1 and not (existing.policy or {}).get("system_boundary_version"):
            existing.policy = {
                **(existing.policy or {}), "no_egress": False,
                "allowed_providers": sorted(SUPPORTED_PROVIDERS),
                "system_boundary_version": 1,
            }
            existing.version += 1
        _bootstrap_starting_profiles(session, existing)
        return existing
    stages, policy = legacy_profile_spec(settings)
    profile = ExecutionProfile(
        name="system-default", description="Imported from config.toml/environment",
        stages=stages, policy=policy, is_system_default=True,
    )
    session.add(profile)

    # Catalog only providers/models actually selected by the imported profile.
    for stage, route in stages.items():
        for candidate in [route, *route.get("fallback", [])]:
            _ensure_catalog_entry(session, candidate["provider"], candidate.get("model"),
                                  STAGE_CAPABILITY.get(stage))
    session.flush()
    _bootstrap_starting_profiles(session, profile)
    return profile


def sync_system_default_profile(session: Session, settings: Settings) -> ExecutionProfile:
    """Explicitly refresh the managed system default from legacy settings.

    This is separate from bootstrap and resolution so ordinary reads never
    overwrite durable profile state. Only the system-default row is changed.
    """
    profile = session.scalar(
        select(ExecutionProfile).where(ExecutionProfile.is_system_default)
    )
    if profile is None:
        raise RuntimeError("no system-default execution profile; run localplaud init")
    stages, policy = legacy_profile_spec(settings)
    profile.stages = stages
    profile.policy = policy
    profile.description = "Imported from config.toml/environment"
    profile.version = (profile.version or 0) + 1
    for stage, route in stages.items():
        for candidate in [route, *route.get("fallback", [])]:
            _ensure_catalog_entry(
                session,
                candidate["provider"],
                candidate.get("model"),
                STAGE_CAPABILITY.get(stage),
            )
    session.flush()
    return profile


def _bootstrap_starting_profiles(session: Session, default: ExecutionProfile) -> None:
    variants = {
        "apple-local": ("mlx-whisper", "mlx-community/whisper-large-v3-turbo",
                        {"device": "mlx"}),
        "nvidia-local": ("faster-whisper", "large-v3-turbo",
                         {"device": "cuda", "compute_type": "float16"}),
        "cpu-local": ("faster-whisper", "large-v3-turbo",
                      {"device": "cpu", "compute_type": "int8"}),
    }
    for name, (provider, model, options) in variants.items():
        if session.scalar(select(ExecutionProfile).where(ExecutionProfile.name == name)):
            continue
        stages = deepcopy(default.stages)
        stages["transcribe"] = {
            "provider": provider, "model": model, "egress": "local_only",
            "options": {"language": "auto", **options}, "fallback": [],
        }
        providers = sorted({route["provider"] for route in stages.values()})
        session.add(ExecutionProfile(
            name=name,
            description="Built-in recommendation; select explicitly after capability health checks",
            stages=stages,
            policy={"no_egress": True, "allowed_providers": providers,
                    "fallback_requires_explicit_configuration": True},
        ))
        _ensure_catalog_entry(session, provider, model, "asr")

    _bootstrap_external_starting_profiles(session, default)


def _bootstrap_external_starting_profiles(
    session: Session, default: ExecutionProfile
) -> None:
    connections = {
        "openai-cloud": ("openai", "https://api.openai.com/v1", "env:OPENAI_API_KEY"),
        "openai-compatible": ("openai", "http://127.0.0.1:8001/v1",
                              "env:OPENAI_COMPATIBLE_API_KEY"),
        "remote-gpu": ("remote-worker", "http://127.0.0.1:8090",
                       "env:LOCALPLAUD_REMOTE_WORKER_TOKEN"),
    }
    for name, (provider_type, base_url, secret_ref) in connections.items():
        _ensure_named_connection(session, name, provider_type, base_url, secret_ref)

    for connection, model, capabilities in (
        ("openai-cloud", "gpt-4o-mini-transcribe", ["asr"]),
        ("openai-cloud", "gpt-4.1-mini", ["correction", "notes", "mind_map", "ask"]),
        ("openai-cloud", "text-embedding-3-small", ["embeddings"]),
        # Deliberately partial: text support never implies audio/embeddings.
        ("openai-compatible", "configure-me-text", ["correction", "notes", "mind_map", "ask"]),
        ("remote-gpu", "large-v3-turbo", ["asr"]),
        ("remote-gpu", "whisperx-align", ["alignment"]),
        ("remote-gpu", "pyannote-community-1", ["diarization"]),
        ("remote-gpu", "worker-text", ["correction", "notes", "mind_map", "ask"]),
        ("remote-gpu", "worker-embeddings", ["embeddings"]),
    ):
        _ensure_named_model(session, connection, model, capabilities)

    profiles: dict[str, dict[str, dict[str, Any]]] = {}
    openai = deepcopy(default.stages)
    for stage in ("correct", "summarize", "mind_map", "ask"):
        openai[stage] = {"provider": "openai", "connection": "openai-cloud",
                         "model": "gpt-4.1-mini", "fallback": []}
    openai["transcribe"] = {"provider": "openai", "connection": "openai-cloud",
                             "model": "gpt-4o-mini-transcribe", "fallback": []}
    openai["index"] = {"provider": "openai", "connection": "openai-cloud",
                        "model": "text-embedding-3-small", "fallback": []}
    profiles["openai-cloud"] = openai

    compatible = deepcopy(default.stages)
    for stage in ("correct", "summarize", "mind_map", "ask"):
        compatible[stage] = {"provider": "openai", "connection": "openai-compatible",
                             "model": "configure-me-text", "fallback": []}
    profiles["openai-compatible"] = compatible

    remote = deepcopy(default.stages)
    remote_models = {
        "transcribe": "large-v3-turbo", "align": "whisperx-align",
        "diarize": "pyannote-community-1", "correct": "worker-text",
        "summarize": "worker-text", "mind_map": "worker-text", "ask": "worker-text",
        "index": "worker-embeddings",
    }
    for stage, model in remote_models.items():
        remote[stage] = {"provider": "remote-worker", "connection": "remote-gpu",
                         "model": model, "fallback": []}
    profiles["remote-gpu"] = remote

    for name, stages in profiles.items():
        if session.scalar(select(ExecutionProfile).where(ExecutionProfile.name == name)):
            continue
        providers = sorted({route["provider"] for route in stages.values()})
        session.add(ExecutionProfile(
            name=name,
            description=(
                "Built-in starter; configure and health-check its connection before selection"
            ),
            stages=stages,
            policy={"no_egress": False, "allowed_providers": providers,
                    "fallback_requires_explicit_configuration": True},
        ))


def _ensure_named_connection(session: Session, name: str, provider_type: str,
                             base_url: str, secret_ref: str) -> ProviderConnection:
    connection = session.scalar(select(ProviderConnection).where(
        ProviderConnection.name == name
    ))
    if connection is None:
        connection = ProviderConnection(
            name=name, provider_type=provider_type, base_url=base_url,
            secret_ref=secret_ref,
            configuration={"starter": True, "egress": _egress(provider_type)},
        )
        session.add(connection)
        session.flush()
    return connection


def _ensure_named_model(session: Session, connection_name: str, model: str,
                        capabilities: list[str]) -> None:
    connection = session.scalar(select(ProviderConnection).where(
        ProviderConnection.name == connection_name
    ))
    assert connection is not None
    row = session.scalar(select(ProviderModel).where(
        ProviderModel.connection_id == connection.id, ProviderModel.name == model,
    ))
    if row is None:
        session.add(ProviderModel(
            connection_id=connection.id, name=model, capabilities=capabilities,
            metadata_={"starter": True},
        ))
    else:
        row.capabilities = sorted(set(row.capabilities) | set(capabilities))


def _ensure_catalog_entry(session: Session, provider: str, model: str | None,
                          capability: str | None) -> None:
    normalized = provider.upper().replace("-", "_")
    default_secret_ref = _DEFAULT_SECRET_REFS.get(
        provider, f"env:LOCALPLAUD_{normalized}_API_KEY"
    )
    connection = session.scalar(select(ProviderConnection).where(
        ProviderConnection.name == provider
    ))
    if connection is None:
        connection = ProviderConnection(
            name=provider, provider_type=provider,
            secret_ref=None if _egress(provider) == "local_only"
            else default_secret_ref,
            base_url=_DEFAULT_BASE_URLS.get(provider),
            configuration={"egress": _egress(provider)},
        )
        session.add(connection)
        session.flush()
    elif _egress(provider) != "local_only":
        old_default = f"env:LOCALPLAUD_{normalized}_API_KEY"
        changed = False
        if connection.secret_ref in {None, old_default} and connection.secret_ref != default_secret_ref:
            connection.secret_ref = default_secret_ref
            changed = True
        if not connection.base_url and provider in _DEFAULT_BASE_URLS:
            connection.base_url = _DEFAULT_BASE_URLS[provider]
            changed = True
        if changed:
            connection.version = (connection.version or 0) + 1
    if not model or not capability:
        return
    row = session.scalar(select(ProviderModel).where(
        ProviderModel.connection_id == connection.id, ProviderModel.name == model,
    ))
    if row is None:
        session.add(ProviderModel(
            connection_id=connection.id, name=model, capabilities=[capability],
            metadata_={"imported_from": "legacy-config"},
        ))
    elif capability not in row.capabilities:
        row.capabilities = [*row.capabilities, capability]
