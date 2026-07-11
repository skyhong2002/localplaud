"""Durable controller-side remote worker registration and health."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import ModelCatalogEntry, ProviderConnection, RemoteWorker
from ..providers.contracts import Capability, Health, StageCapabilities
from .client import RemoteWorkerClient


def list_workers(session: Session) -> list[dict]:
    return [
        {
            "id": row.id,
            "key": row.key,
            "name": row.name,
            "base_url": row.base_url,
            "token_env": row.token_env,
            "protocol_version": row.protocol_version,
            "capabilities": row.capabilities,
            "health": row.health,
            "enabled": row.enabled,
        }
        for row in session.scalars(select(RemoteWorker).order_by(RemoteWorker.id))
    ]


def save_worker(session: Session, data: dict, worker_id: int | None = None) -> dict:
    row = session.get(RemoteWorker, worker_id) if worker_id else None
    if worker_id and row is None:
        raise LookupError("remote worker not found")
    if row is None:
        row = RemoteWorker(key=data["key"], name=data["name"], base_url=data["base_url"])
        session.add(row)
    for field in ("key", "name", "base_url", "token_env", "enabled"):
        if field in data:
            setattr(row, field, data[field])
    session.flush()

    connection_key = f"worker:{row.key}"
    connection = session.scalar(
        select(ProviderConnection).where(ProviderConnection.key == connection_key)
    )
    config = {
        "base_url": row.base_url,
        "token_env": row.token_env,
        "timeout": data.get("timeout", 120),
        "job_timeout": data.get("job_timeout", 3600),
    }
    if connection is None:
        connection = ProviderConnection(
            key=connection_key,
            name=row.name,
            provider_type="localplaud-worker",
            execution_target="remote_worker",
            data_egress=True,
            secret_ref=f"env:{row.token_env}",
            config=config,
        )
        session.add(connection)
    else:
        connection.name = row.name
        connection.secret_ref = f"env:{row.token_env}"
        connection.config = config
    session.flush()
    return next(item for item in list_workers(session) if item["id"] == row.id)


def check_worker(session: Session, worker_id: int) -> dict:
    row = session.get(RemoteWorker, worker_id)
    if row is None:
        raise LookupError("remote worker not found")
    checked_at = datetime.now(UTC).isoformat()
    try:
        client = RemoteWorkerClient.from_config(
            {"base_url": row.base_url, "token_env": row.token_env}
        )
        try:
            handshake = client.handshake()
        finally:
            client.close()
        row.protocol_version = handshake.version
        row.capabilities = handshake.model_dump(mode="json")["capabilities"]
        row.health = {"status": "healthy", "checked_at": checked_at, "detail": handshake.worker_id}
        connection = session.scalar(
            select(ProviderConnection).where(ProviderConnection.key == f"worker:{row.key}")
        )
        stages_by_model: dict[str, list] = {}
        for stage in handshake.capabilities:
            for model_name in stage.models:
                stages_by_model.setdefault(model_name, []).append(stage.stage)
        for model_name, stages in stages_by_model.items():
            model = session.scalar(
                select(ModelCatalogEntry).where(
                    ModelCatalogEntry.connection_id == connection.id,
                    ModelCatalogEntry.model_key == model_name,
                )
            )
            capability = Capability(
                execution_target="remote_worker",
                data_egress=True,
                health=Health(status="healthy", checked_at=checked_at),
                stages=tuple(StageCapabilities(stage=stage) for stage in stages),
            ).model_dump(mode="json")
            if model is None:
                session.add(
                    ModelCatalogEntry(
                        connection_id=connection.id,
                        model_key=model_name,
                        display_name=model_name,
                        capabilities=capability,
                    )
                )
            else:
                model.capabilities = capability
    except Exception as exc:  # noqa: BLE001
        row.health = {"status": "unavailable", "checked_at": checked_at, "detail": str(exc)}
    session.flush()
    return row.health


def delete_worker(session: Session, worker_id: int) -> None:
    row = session.get(RemoteWorker, worker_id)
    if row is None:
        raise LookupError("remote worker not found")
    connection = session.scalar(
        select(ProviderConnection).where(ProviderConnection.key == f"worker:{row.key}")
    )
    if connection is not None:
        from ..providers.service import delete_connection, delete_model

        for model in list(
            session.scalars(
                select(ModelCatalogEntry).where(
                    ModelCatalogEntry.connection_id == connection.id
                )
            )
        ):
            delete_model(session, model.id)
        delete_connection(session, connection.id)
    session.delete(row)
