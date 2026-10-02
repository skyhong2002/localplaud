"""Opt-in cloud and remote starting profiles.

Each starter clones the current system default profile and replaces only the
stages it is about, so everything else (ASR, alignment, diarization, embeddings,
cost ceiling) keeps the user's existing choice. Starters are never made the
system default, require an explicit egress acknowledgement, and only accept
secret *references* (environment variable names).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import ModelCatalogEntry, ProviderConnection, RemoteWorker
from .contracts import ProviderStage

TEXT_STAGES = ("correct", "summarize", "mind_map", "ask")
SPEECH_STAGES = ("transcribe", "diarize")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")

STARTING_PROFILES: list[dict[str, Any]] = [
    {
        "kind": "openai-cloud",
        "name": "OpenAI Cloud",
        "stages": list(TEXT_STAGES),
        "egress": (
            "Corrections, notes, mind maps and Ask send transcript text, speaker names "
            "and your questions to OpenAI. Audio, transcription and speaker "
            "diarization stay on this host."
        ),
    },
    {
        "kind": "openai-compatible",
        "name": "OpenAI-compatible endpoint",
        "stages": list(TEXT_STAGES),
        "egress": (
            "Corrections, notes, mind maps and Ask send transcript text, speaker names "
            "and your questions to the endpoint you enter. It is treated as leaving "
            "this host even if it runs on your network."
        ),
    },
    {
        "kind": "remote-gpu",
        "name": "Remote GPU worker",
        "stages": list(SPEECH_STAGES),
        "egress": (
            "Recording audio is uploaded to the selected localplaud worker for "
            "transcription and speaker diarization. Other stages keep their current "
            "providers."
        ),
    },
]


def _default_profile(session: Session) -> dict:
    from .service import list_profiles

    default = next((item for item in list_profiles(session) if item["is_system_default"]), None)
    if default is None:
        raise ValueError("no system default execution profile")
    return default


def _env_ref(name: str | None) -> str:
    value = (name or "").strip().removeprefix("env:")
    if not _ENV_NAME.fullmatch(value):
        raise ValueError("enter the name of an environment variable, e.g. OPENAI_API_KEY")
    return f"env:{value}"


def _text_connection(
    session: Session, key: str, name: str, base_url: str, secret_ref: str, model: str
) -> tuple[str, str]:
    from .service import _capability, save_connection, save_model

    connection = session.scalar(select(ProviderConnection).where(ProviderConnection.key == key))
    data = {
        "key": key,
        "name": name,
        "provider_type": "openai",
        "execution_target": "cloud",
        "data_egress": True,
        "secret_ref": secret_ref,
        "config": {"base_url": base_url},
    }
    connection_id = save_connection(session, data, connection.id if connection else None)["id"]
    existing = session.scalar(
        select(ModelCatalogEntry).where(
            ModelCatalogEntry.connection_id == connection_id,
            ModelCatalogEntry.model_key == model,
        )
    )
    if existing is None:
        save_model(
            session,
            {
                "connection_id": connection_id,
                "model_key": model,
                "display_name": model,
                "capabilities": _capability(
                    [ProviderStage(stage) for stage in TEXT_STAGES], cloud=True
                ),
            },
        )
    return key, model


def install_starting_profile(
    session: Session,
    kind: str,
    *,
    acknowledge_egress: bool,
    secret_env: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    worker_key: str | None = None,
) -> dict:
    from .service import create_profile_version

    spec = next((item for item in STARTING_PROFILES if item["kind"] == kind), None)
    if spec is None:
        raise LookupError("starting profile not found")
    if not acknowledge_egress:
        raise ValueError("confirm what data leaves this host before creating this profile")
    replacements: dict[str, tuple[str, str]] = {}
    if kind == "openai-cloud":
        selection = _text_connection(
            session,
            "llm:openai-cloud",
            "OpenAI Cloud",
            "https://api.openai.com/v1",
            _env_ref(secret_env or "OPENAI_API_KEY"),
            (model or "").strip() or get_settings().llm.openai.model,
        )
        replacements = dict.fromkeys(TEXT_STAGES, selection)
    elif kind == "openai-compatible":
        parsed = urlparse((base_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("enter the endpoint base URL, e.g. https://llm.example/v1")
        if parsed.username or parsed.password or parsed.query:
            raise ValueError("the endpoint URL must not contain credentials or a query")
        if not (model or "").strip():
            raise ValueError("enter the model identifier served by the endpoint")
        # Port and path are part of the identity: two endpoints on one host must
        # not overwrite each other's base URL and secret reference.
        endpoint = parsed.hostname.lower()
        if parsed.port:
            endpoint += f"-{parsed.port}"
        endpoint += parsed.path.rstrip("/")
        host = re.sub(r"[^a-z0-9.-]+", "-", endpoint).strip("-")[:64]
        selection = _text_connection(
            session,
            f"llm:compatible:{host}",
            f"OpenAI-compatible · {parsed.hostname}",
            (base_url or "").strip().rstrip("/"),
            _env_ref(secret_env),
            model.strip(),
        )
        replacements = dict.fromkeys(TEXT_STAGES, selection)
    else:
        worker = session.scalar(select(RemoteWorker).where(RemoteWorker.key == worker_key))
        if worker is None:
            raise LookupError("remote worker not found")
        if not worker.enabled:
            raise ValueError("this remote worker is revoked; re-enable it first")
        connection_key = f"worker:{worker.key}"
        for capability in worker.capabilities or []:
            stage = capability.get("stage")
            models = capability.get("models") or []
            if stage in SPEECH_STAGES and models:
                replacements[stage] = (connection_key, models[0])
        if not replacements:
            raise ValueError(
                "this worker has not reported transcription or diarization models; "
                "run Test on the worker first"
            )

    default = _default_profile(session)
    stages = {
        stage: {
            "connection": selection["connection"],
            "model": selection["model"],
            "options": dict(selection.get("options") or {}),
        }
        for stage, selection in default["stages"].items()
    }
    for stage, (connection_key, model_key) in replacements.items():
        stages[stage] = {"connection": connection_key, "model": model_key, "options": {}}
    policy = default["policy"]
    fallbacks = {
        stage: candidates
        for stage, candidates in ((policy.get("fallback_policy") or {}).get("stages") or {}).items()
        if stage not in replacements
    }
    return create_profile_version(
        session,
        {
            "key": f"starter-{kind}",
            "name": spec["name"],
            # Never auto-selected: the user picks it per folder/template/recording
            # or promotes it explicitly by creating a default version.
            "is_system_default": False,
            "privacy_policy": "allow-egress",
            "no_egress": False,
            "cost_ceiling": policy.get("cost_ceiling"),
            "quality_floor": policy.get("quality_floor") or {},
            "fallback_policy": {"stages": fallbacks},
            "stages": stages,
        },
    )
