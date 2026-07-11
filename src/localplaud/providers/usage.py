"""Normalized stage usage and catalog-driven cost estimation."""

from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ModelCatalogEntry, ProviderConnection, StageAttempt


class CostPolicyError(RuntimeError):
    """Raised before egress when a profile cost boundary cannot be satisfied."""


def normalize_usage(usage: dict | None) -> dict:
    value = dict(usage or {})
    if "input_tokens" not in value and value.get("input_chars") is not None:
        value["input_tokens"] = math.ceil(max(0, value["input_chars"]) / 4)
        value["tokens_estimated"] = True
    if "output_tokens" not in value and value.get("output_chars") is not None:
        value["output_tokens"] = math.ceil(max(0, value["output_chars"]) / 4)
        value["tokens_estimated"] = True
    if value.get("audio_seconds") is not None:
        value["audio_seconds"] = round(max(0.0, float(value["audio_seconds"])), 3)
    return value


def pricing_for_stage(session: Session, snapshot: dict | None, stage: str) -> dict:
    selection = (snapshot or {}).get("stages", {}).get(stage) or {}
    connection_key, model_key = selection.get("connection"), selection.get("model")
    if not connection_key or not model_key:
        return {}
    connection = session.scalar(
        select(ProviderConnection).where(ProviderConnection.key == connection_key)
    )
    if connection is None:
        return {}
    model = session.scalar(
        select(ModelCatalogEntry).where(
            ModelCatalogEntry.connection_id == connection.id,
            ModelCatalogEntry.model_key == model_key,
        )
    )
    if model is None:
        return {}
    return ((model.capabilities or {}).get("metadata") or {}).get("pricing") or {}


def estimate_cost(usage: dict, pricing: dict) -> float:
    """Estimate USD from explicit catalog rates; missing rates always mean zero."""
    normalized = normalize_usage(usage)
    total = float(pricing.get("per_request_usd") or 0) * max(
        1, int(normalized.get("requests") or 1)
    )
    total += (
        float(normalized.get("input_tokens") or 0)
        / 1_000_000
        * float(pricing.get("input_per_million_tokens_usd") or 0)
    )
    total += (
        float(normalized.get("output_tokens") or 0)
        / 1_000_000
        * float(pricing.get("output_per_million_tokens_usd") or 0)
    )
    total += (
        float(normalized.get("audio_seconds") or 0)
        / 60
        * float(pricing.get("audio_per_minute_usd") or 0)
    )
    return round(total, 8)


def cost_budget_status(session: Session, file_id: str, snapshot: dict | None) -> dict:
    ceiling = ((snapshot or {}).get("policy") or {}).get("cost_ceiling")
    from sqlalchemy import func

    spent = float(
        session.scalar(
            select(func.coalesce(func.sum(StageAttempt.estimated_cost_usd), 0)).where(
                StageAttempt.file_id == file_id
            )
        )
        or 0
    )
    return {
        "ceiling_usd": ceiling,
        "spent_usd": round(spent, 8),
        "remaining_usd": (None if ceiling is None else round(max(0.0, float(ceiling) - spent), 8)),
        "exceeded": ceiling is not None and spent > float(ceiling),
    }


def enforce_cost_ceiling(
    session: Session,
    file_id: str,
    stage: str,
    snapshot: dict | None,
    projected_usage: dict,
) -> dict:
    """Reserve a conservative priced stage estimate before provider invocation."""
    budget = cost_budget_status(session, file_id, snapshot)
    if budget["ceiling_usd"] is None:
        return budget | {"projected_usd": 0.0, "enforced": False}
    selection = (snapshot or {}).get("stages", {}).get(stage) or {}
    pricing = pricing_for_stage(session, snapshot, stage)
    external = selection.get("execution_target") in {"cloud", "remote_worker"}
    if external and not pricing:
        raise CostPolicyError(
            f"{stage} cost is unknown for {selection.get('connection')}:{selection.get('model')}; "
            "add model pricing or mark it free before data leaves this host"
        )
    projected = estimate_cost(projected_usage, pricing)
    total = budget["spent_usd"] + projected
    if total > float(budget["ceiling_usd"]) + 1e-12:
        raise CostPolicyError(
            f"{stage} would exceed the ${float(budget['ceiling_usd']):.6g} recording ceiling "
            f"(${budget['spent_usd']:.6g} spent + ${projected:.6g} projected)"
        )
    return budget | {
        "projected_usd": projected,
        "after_projection_usd": round(total, 8),
        "enforced": True,
        "pricing": pricing,
    }
