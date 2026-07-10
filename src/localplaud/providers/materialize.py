"""Merge durable connection/model records into a resolved route snapshot."""

from __future__ import annotations

import ipaddress
from copy import deepcopy
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ProviderConnection, ProviderModel
from .catalog import provider_egress
from .resolver import ProfileResolutionError

_CAPABILITY = {
    "transcribe": "asr", "align": "alignment", "diarize": "diarization",
    "correct": "correction", "summarize": "notes", "mind_map": "mind_map",
    "ask": "ask", "index": "embeddings",
}
_SAFE_CONNECTION_OPTIONS = {
    "headers", "timeout_seconds", "max_input_bytes", "max_input_chars",
    "input_limit", "prompt_limit", "trusted_single_user", "poll_interval_seconds", "dim",
}


def materialize_snapshot(session: Session, snapshot: dict) -> dict:
    result = deepcopy(snapshot)
    stage = result["stage"]
    result["route"] = _materialize_route(session, stage, result["route"])
    result["route"]["fallback"] = [
        _materialize_route(session, stage, item)
        for item in result["route"].get("fallback", [])
    ]
    policy = result.get("policy", {})
    if policy.get("no_egress") and any(
        route["egress"] != "local_only"
        for route in [result["route"], *result["route"].get("fallback", [])]
    ):
        raise ProfileResolutionError("materialized route violates no-egress policy")
    return result


def _materialize_route(session: Session, stage: str, route: dict) -> dict:
    route = deepcopy(route)
    connection_name = route.get("connection", route.get("provider"))
    connection = session.scalar(select(ProviderConnection).where(
        ProviderConnection.name == connection_name, ProviderConnection.enabled,
    ))
    if connection is None:
        raise ProfileResolutionError(f"enabled provider connection {connection_name!r} not found")
    requested_provider = route.get("provider")
    if route.get("connection") and requested_provider not in {
        connection.name, connection.provider_type,
    }:
        raise ProfileResolutionError("route provider does not match its connection type")
    route["connection"] = connection.name
    route["connection_version"] = connection.version
    route["provider"] = connection.provider_type
    egress = provider_egress(connection.provider_type, stage).value
    if connection.base_url:
        hostname = urlparse(connection.base_url).hostname
        try:
            if hostname == "localhost" or (hostname and ipaddress.ip_address(hostname).is_loopback):
                egress = "local_only"
        except ValueError:
            pass
    route["egress"] = egress
    if connection.base_url:
        route["base_url"] = connection.base_url
    if connection.secret_ref:
        route["secret_ref"] = connection.secret_ref
    for key in _SAFE_CONNECTION_OPTIONS:
        if key in (connection.configuration or {}) and key not in route:
            route[key] = connection.configuration[key]

    model_name = route.get("model")
    if model_name:
        model = session.scalar(select(ProviderModel).where(
            ProviderModel.connection_id == connection.id,
            ProviderModel.name == model_name, ProviderModel.enabled,
        ))
        capability = _CAPABILITY.get(stage)
        if model is None or (capability and capability not in model.capabilities):
            raise ProfileResolutionError(
                f"model {model_name!r} does not declare {capability!r} capability"
            )
        route["model_id"] = model.id
        route["model_version"] = model.version
        route["limits"] = deepcopy(model.limits or {})
        for key, value in (model.limits or {}).items():
            if key in {"max_input_bytes", "max_input_chars", "input_limit", "prompt_limit",
                       "dim", "timeout_seconds"}:
                route.setdefault(key, value)
    return route
