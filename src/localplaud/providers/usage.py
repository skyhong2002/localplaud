"""Normalized stage usage and catalog-driven cost estimation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session

from ..db.models import (
    AskMessage,
    AskThread,
    KnowledgeIndexAttempt,
    ModelCatalogEntry,
    PlaudFile,
    ProviderConnection,
    ProviderCostReservation,
    StageAttempt,
    StageStatus,
)


class CostPolicyError(RuntimeError):
    """Raised before egress when a profile cost boundary cannot be satisfied."""


_PROVIDER_DISPATCH_LEASE = timedelta(hours=24)


def provider_dispatch_owner() -> str:
    """Return a durable owner for daemon, CLI, and web-process dispatches."""
    from ..poller.poll import current_daemon_owner

    return current_daemon_owner() or f"process:{os.getpid()}"


def provider_dispatch_fingerprint(snapshot: dict, operation: str) -> str:
    """Identify the exact non-secret stage and policy authorized for egress."""
    payload = {
        "operation": operation,
        "selection": (snapshot.get("stages") or {}).get(operation) or {},
        "policy": snapshot.get("policy") or {},
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def process_peak_memory_mb() -> float | None:
    """Return this worker process's RSS high-water mark, when the OS exposes it.

    macOS reports bytes while Linux/BSD report KiB. This is deliberately process-level
    telemetry, not a claim about memory exclusively owned by one model or stage.
    """
    try:
        import resource

        raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ImportError, OSError, ValueError):
        return None
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(raw / divisor, 3) if raw >= 0 else None


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


def _cost_scope_key(file_id: str | None) -> str:
    return f"file:{file_id}" if file_id is not None else "library"


def lock_cost_budget(session: Session, file_id: str | None) -> None:
    """Serialize reservations sharing one recording or library budget."""
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        # A no-op DML statement acquires SQLite's database-wide write lock and
        # remains safe when the caller already has an active transaction.
        session.execute(
            text("UPDATE plaud_files SET updated_at = updated_at WHERE id = :file_id"),
            {"file_id": file_id},
        )
    elif dialect == "postgresql":
        if file_id is None:
            session.execute(text("SELECT pg_advisory_xact_lock(1280330574)"))
        else:
            # Global provider/profile mutations take the matching exclusive
            # advisory lock before recording rows. Every recording budget must
            # therefore take the shared lock first, including callers that reach
            # the budget before a processing-claim assertion.
            session.execute(text("SELECT pg_advisory_xact_lock_shared(1280330574)"))
            session.scalar(
                select(PlaudFile.id)
                .where(PlaudFile.id == file_id)
                .with_for_update()
            )


def cost_budget_status(
    session: Session, file_id: str | None, snapshot: dict | None
) -> dict:
    ceiling = ((snapshot or {}).get("policy") or {}).get("cost_ceiling")
    stage_spent = (
        float(
            session.scalar(
                select(func.coalesce(func.sum(StageAttempt.estimated_cost_usd), 0)).where(
                    StageAttempt.file_id == file_id,
                    StageAttempt.status != StageStatus.running,
                )
            )
            or 0
        )
        if file_id is not None
        else 0.0
    )
    note_condition = (
        KnowledgeIndexAttempt.file_id == file_id
        if file_id is not None
        else KnowledgeIndexAttempt.file_id.is_(None)
    )
    note_spent = float(
        session.scalar(
            select(
                func.coalesce(func.sum(KnowledgeIndexAttempt.estimated_cost_usd), 0)
            ).where(note_condition, KnowledgeIndexAttempt.status != "running")
        )
        or 0
    )
    ask_condition = (
        AskThread.file_id == file_id
        if file_id is not None
        else AskThread.file_id.is_(None)
    )
    ask_spent = float(
        session.scalar(
            select(func.coalesce(func.sum(AskMessage.estimated_cost_usd), 0))
            .join(AskThread, AskThread.id == AskMessage.thread_id)
            .where(AskMessage.role == "assistant", ask_condition)
        )
        or 0
    )
    reserved = float(
        session.scalar(
            select(
                func.coalesce(func.sum(ProviderCostReservation.estimated_cost_usd), 0)
            ).where(
                ProviderCostReservation.scope_key == _cost_scope_key(file_id)
            )
        )
        or 0
    )
    spent = stage_spent + note_spent + ask_spent + reserved
    return {
        "ceiling_usd": ceiling,
        "spent_usd": round(spent, 8),
        "stage_spent_usd": round(stage_spent, 8),
        "note_spent_usd": round(note_spent, 8),
        "ask_spent_usd": round(ask_spent, 8),
        "reserved_usd": round(reserved, 8),
        "remaining_usd": (None if ceiling is None else round(max(0.0, float(ceiling) - spent), 8)),
        "exceeded": ceiling is not None and spent > float(ceiling),
    }


def reserve_provider_cost(
    session: Session,
    *,
    reservation_id: str,
    file_id: str | None,
    operation: str,
    snapshot: dict,
    usage: dict,
    additional_spent_usd: float = 0.0,
) -> tuple[float, dict]:
    """Atomically reserve one provider call against the shared durable ledger."""
    lock_cost_budget(session, file_id)
    selection = snapshot["stages"][operation]
    pricing = pricing_for_stage(session, snapshot, operation)
    ceiling = (snapshot.get("policy") or {}).get("cost_ceiling")
    external = selection.get("execution_target") in {"cloud", "remote_worker"}
    if ceiling is not None and external and not pricing:
        raise CostPolicyError(
            f"{operation} cost is unknown for "
            f"{selection.get('connection')}:{selection.get('model')}"
        )
    projected = estimate_cost(usage, pricing)
    row = session.get(ProviderCostReservation, reservation_id)
    current = float(row.estimated_cost_usd or 0) if row is not None else 0.0
    budget = cost_budget_status(session, file_id, snapshot)
    total = budget["spent_usd"] + float(additional_spent_usd) + projected
    if ceiling is not None and total > float(ceiling) + 1e-12:
        raise CostPolicyError(
            f"{operation} would exceed the ${float(ceiling):.6g} cost ceiling "
            f"(${budget['spent_usd'] + float(additional_spent_usd):.6g} spent + "
            f"${projected:.6g} projected)"
        )
    if row is None and ceiling is None and projected <= 0 and not external:
        return projected, pricing
    now = datetime.now(UTC)
    if row is None:
        row = ProviderCostReservation(
            id=reservation_id,
            scope_key=_cost_scope_key(file_id),
            file_id=file_id,
            operation=operation,
            status="active",
            usage={"reservations": []},
        )
        session.add(row)
    reservation = normalize_usage(usage) | {
        "projected_usd": projected,
        "connection": selection.get("connection"),
        "model": selection.get("model"),
    }
    prior = list((row.usage or {}).get("reservations") or [])
    row.usage = {"reservations": prior + [reservation]}
    row.estimated_cost_usd = current + projected
    row.provider = (selection.get("connection") or "").split(":", 1)[-1] or None
    row.model = selection.get("model")
    row.status = "active"
    row.owner = provider_dispatch_owner()
    row.lease_until = now + _PROVIDER_DISPATCH_LEASE
    row.profile_fingerprint = provider_dispatch_fingerprint(snapshot, operation)
    row.completed_at = None
    return projected, pricing


def finalize_provider_cost_reservations(
    session: Session,
    reservation_ids: list[str],
    *,
    status: str,
    release: bool = False,
) -> None:
    if not reservation_ids:
        return
    if release:
        session.execute(
            delete(ProviderCostReservation).where(
                ProviderCostReservation.id.in_(reservation_ids)
            )
        )
        return
    now = datetime.now(UTC)
    for row in session.scalars(
        select(ProviderCostReservation).where(
            ProviderCostReservation.id.in_(reservation_ids)
        )
    ):
        row.status = status
        row.lease_until = None
        row.completed_at = now


def recover_provider_dispatch_reservations(previous_owner: str | None) -> int:
    """Close expired dispatches and leases owned by a replaced daemon.

    Stage and note-index attempts are the durable spend ledger.  A reservation
    linked from one of those attempts must be folded into that attempt and
    deleted, otherwise both rows are counted against the same cost ceiling after
    restart.  Ask reservations have no assistant message until publication, so
    they remain as one failed reservation when their request is interrupted.
    """
    now = datetime.now(UTC)
    from ..db.session import session_scope

    with session_scope() as session:
        # Recovery moves spend between two rows in the same cost ledger.  Take
        # the library fence first so a concurrent PostgreSQL reservation cannot
        # read the gap between the reservation and its durable attempt.
        lock_cost_budget(session, None)
        reclaimable = or_(
            ProviderCostReservation.lease_until.is_(None),
            ProviderCostReservation.lease_until <= now,
        )
        if previous_owner:
            reclaimable = or_(
                reclaimable,
                ProviderCostReservation.owner == previous_owner,
            )
        reservations = list(
            session.scalars(
                select(ProviderCostReservation).where(
                    ProviderCostReservation.status == "active",
                    reclaimable,
                )
            )
        )
        if not reservations:
            return 0

        for file_id in sorted(
            {
                reservation.file_id
                for reservation in reservations
                if reservation.file_id is not None
            }
        ):
            lock_cost_budget(session, file_id)

        linked_attempts: dict[str, StageAttempt | KnowledgeIndexAttempt] = {}
        for attempt in session.scalars(select(StageAttempt)):
            for reservation_id in (attempt.usage or {}).get(
                "dispatch_reservation_ids", []
            ):
                linked_attempts[str(reservation_id)] = attempt
        for attempt in session.scalars(select(KnowledgeIndexAttempt)):
            for reservation_id in (attempt.usage or {}).get(
                "dispatch_reservation_ids", []
            ):
                linked_attempts[str(reservation_id)] = attempt

        linked_costs: dict[tuple[type, int], float] = {}
        linked_rows: dict[tuple[type, int], StageAttempt | KnowledgeIndexAttempt] = {}
        for reservation in reservations:
            attempt = linked_attempts.get(reservation.id)
            if attempt is None:
                reservation.status = "failed"
                reservation.lease_until = None
                reservation.completed_at = now
                continue
            key = (type(attempt), attempt.id)
            linked_rows[key] = attempt
            linked_costs[key] = linked_costs.get(key, 0.0) + float(
                reservation.estimated_cost_usd or 0
            )
            session.delete(reservation)

        for key, attempt in linked_rows.items():
            attempt.estimated_cost_usd = max(
                float(attempt.estimated_cost_usd or 0), linked_costs[key]
            )
            if isinstance(attempt, KnowledgeIndexAttempt) and attempt.status == "running":
                attempt.status = "failed"
                attempt.error = "provider dispatch interrupted by process restart"
                attempt.completed_at = now
        return len(reservations)


def provider_cost_reservation_total(
    session: Session, reservation_ids: list[str]
) -> float:
    if not reservation_ids:
        return 0.0
    return round(
        float(
            session.scalar(
                select(
                    func.coalesce(
                        func.sum(ProviderCostReservation.estimated_cost_usd), 0
                    )
                ).where(ProviderCostReservation.id.in_(reservation_ids))
            )
            or 0
        ),
        8,
    )


def enforce_cost_ceiling(
    session: Session,
    file_id: str,
    stage: str,
    snapshot: dict | None,
    projected_usage: dict,
) -> dict:
    """Reserve a conservative priced stage estimate before provider invocation."""
    lock_cost_budget(session, file_id)
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
    attempt = session.scalar(
        select(StageAttempt)
        .where(
            StageAttempt.file_id == file_id,
            StageAttempt.stage == stage,
            StageAttempt.status == StageStatus.running,
        )
        .order_by(StageAttempt.attempt.desc())
    )
    if attempt is not None:
        attempt.usage = normalize_usage(projected_usage) | {"projection": True}
        attempt.estimated_cost_usd = projected
    return budget | {
        "projected_usd": projected,
        "after_projection_usd": round(total, 8),
        "enforced": True,
        "pricing": pricing,
    }
