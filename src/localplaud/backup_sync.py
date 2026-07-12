"""Authorized, durable cross-host backup uploads."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

import httpx
from sqlalchemy import select

from .backups import file_sha256, workspace_backup_path
from .db.models import BackupDestination, BackupSyncDelivery
from .db.session import session_scope

_ENV_REF = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
MAX_RESPONSE_BYTES = 64 * 1024


def validate_destination_url(url: str, *, allow_private_network: bool) -> None:
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "backup destination URL must not contain credentials, query parameters, or a fragment"
        )
    if not parsed.hostname or parsed.scheme not in {"http", "https"}:
        raise ValueError("backup destination must be an absolute HTTP(S) URL")
    if parsed.scheme != "https" and not allow_private_network:
        raise ValueError("public backup destinations require HTTPS")
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        )
    except socket.gaierror as exc:
        raise ValueError("backup destination hostname could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        unsafe = (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
        if unsafe and not allow_private_network:
            raise ValueError("backup destination resolves to a private or non-routable address")


def _secret_value(secret_ref: str | None) -> str | None:
    if not secret_ref:
        return None
    if not _ENV_REF.fullmatch(secret_ref):
        raise ValueError("backup destination secret reference must use env:VARIABLE")
    name = secret_ref.removeprefix("env:")
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"backup destination secret environment variable is missing: {name}")
    return value


def serialize_destination(row: BackupDestination) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "secret_ref": row.secret_ref,
        "enabled": row.enabled,
        "allow_private_network": row.allow_private_network,
        "health": row.health or {},
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def list_destinations(session) -> list[dict]:
    return [
        serialize_destination(row)
        for row in session.scalars(select(BackupDestination).order_by(BackupDestination.name))
    ]


def save_destination(session, data: dict, destination_id: int | None = None) -> dict:
    secret_ref = data.get("secret_ref")
    if secret_ref and not _ENV_REF.fullmatch(secret_ref):
        raise ValueError("backup destination secret reference must use env:VARIABLE")
    allow_private = bool(data.get("allow_private_network"))
    validate_destination_url(data["url"], allow_private_network=allow_private)
    row = session.get(BackupDestination, destination_id) if destination_id else None
    if destination_id and row is None:
        raise LookupError("backup destination not found")
    if row is None:
        row = BackupDestination(name=data["name"], url=data["url"])
        session.add(row)
    row.name = data["name"].strip()
    row.url = data["url"].rstrip("/")
    row.secret_ref = secret_ref
    row.enabled = bool(data.get("enabled", True))
    row.allow_private_network = allow_private
    session.flush()
    return serialize_destination(row)


def destination_snapshot(row: BackupDestination) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "secret_ref": row.secret_ref,
        "enabled": row.enabled,
        "allow_private_network": row.allow_private_network,
    }


def _headers(snapshot: dict, delivery_id: str | None = None) -> dict:
    headers = {"user-agent": "localplaud-backup-sync/1"}
    if delivery_id:
        headers["x-localplaud-delivery-id"] = delivery_id
    if token := _secret_value(snapshot.get("secret_ref")):
        headers["authorization"] = f"Bearer {token}"
    return headers


def _capture_response(response: httpx.Response) -> str:
    captured = bytearray()
    for chunk in response.iter_bytes():
        remaining = MAX_RESPONSE_BYTES - len(captured)
        if remaining <= 0:
            break
        captured.extend(chunk[:remaining])
    return bytes(captured).decode("utf-8", errors="replace")


def test_destination(session, destination_id: int) -> dict:
    row = session.get(BackupDestination, destination_id)
    if row is None:
        raise LookupError("backup destination not found")
    snapshot = destination_snapshot(row)
    checked_at = datetime.now(UTC).isoformat()
    try:
        validate_destination_url(
            snapshot["url"],
            allow_private_network=bool(snapshot.get("allow_private_network")),
        )
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.options(snapshot["url"], headers=_headers(snapshot))
        if not 200 <= response.status_code < 400:
            raise RuntimeError(f"backup destination returned HTTP {response.status_code}")
        row.health = {
            "status": "healthy",
            "detail": f"OPTIONS HTTP {response.status_code}; no backup data sent",
            "checked_at": checked_at,
        }
    except Exception as exc:  # noqa: BLE001 - persisted health result
        row.health = {"status": "unavailable", "detail": str(exc), "checked_at": checked_at}
    session.flush()
    return row.health


def delete_destination(session, destination_id: int) -> None:
    row = session.get(BackupDestination, destination_id)
    if row is None:
        raise LookupError("backup destination not found")
    for delivery in session.scalars(
        select(BackupSyncDelivery).where(BackupSyncDelivery.destination_id == destination_id)
    ):
        delivery.destination_id = None
    session.delete(row)


def serialize_delivery(row: BackupSyncDelivery) -> dict:
    return {
        "id": row.id,
        "destination_id": row.destination_id,
        "destination_name": (row.destination_snapshot or {}).get("name"),
        "backup_name": row.backup_name,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "backup_sha256": row.backup_sha256,
        "size_bytes": row.size_bytes,
        "response_status": row.response_status,
        "error": row.error,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def list_deliveries(session, limit: int = 100) -> list[dict]:
    return [
        serialize_delivery(row)
        for row in session.scalars(
            select(BackupSyncDelivery)
            .order_by(BackupSyncDelivery.created_at.desc())
            .limit(min(max(limit, 1), 500))
        )
    ]


def _upload(snapshot: dict, path, checksum: str, delivery_id: str) -> tuple[int, str]:
    validate_destination_url(
        snapshot["url"], allow_private_network=bool(snapshot.get("allow_private_network"))
    )
    upload_url = snapshot["url"].rstrip("/") + "/" + quote(path.name, safe="")
    headers = _headers(snapshot, delivery_id) | {
        "content-type": "application/zip",
        "content-length": str(path.stat().st_size),
        "x-localplaud-backup-sha256": checksum,
    }
    with path.open("rb") as handle, httpx.Client(timeout=300, follow_redirects=False) as client:
        with client.stream("PUT", upload_url, content=handle, headers=headers) as response:
            return response.status_code, _capture_response(response)


def deliver_backup(backup_name: str, destination_id: int) -> dict:
    path = workspace_backup_path(backup_name)
    checksum = file_sha256(path)
    idempotency_key = f"backup-{checksum}-destination-{destination_id}"
    with session_scope() as session:
        destination = session.get(BackupDestination, destination_id)
        if destination is None:
            raise ValueError("authorized backup destination not found")
        if not destination.enabled:
            raise ValueError("backup destination is disabled")
        snapshot = destination_snapshot(destination)
        row = session.scalar(
            select(BackupSyncDelivery).where(
                BackupSyncDelivery.idempotency_key == idempotency_key
            )
        )
        if row is None:
            row = BackupSyncDelivery(
                destination_id=destination_id,
                backup_name=backup_name,
                idempotency_key=idempotency_key,
                destination_snapshot=snapshot,
                backup_sha256=checksum,
                size_bytes=path.stat().st_size,
            )
            session.add(row)
            session.flush()
        if row.status == "completed":
            return serialize_delivery(row)
        row.status = "running"
        row.attempt_count += 1
        row.error = None
        delivery_row_id = row.id

    response_status = None
    response_excerpt = None
    try:
        response_status, response_excerpt = _upload(
            snapshot, path, checksum, idempotency_key
        )
        if not 200 <= response_status < 300:
            raise RuntimeError(f"backup upload returned HTTP {response_status}")
        with session_scope() as session:
            row = session.get(BackupSyncDelivery, delivery_row_id)
            row.status = "completed"
            row.response_status = response_status
            row.response_excerpt = response_excerpt[:1000]
            row.error = None
            destination = session.get(BackupDestination, destination_id)
            if destination is not None:
                destination.last_used_at = datetime.now(UTC)
                destination.health = {
                    "status": "healthy",
                    "detail": f"last upload HTTP {response_status}",
                    "checked_at": datetime.now(UTC).isoformat(),
                }
            result = serialize_delivery(row)
        return result
    except Exception as exc:  # noqa: BLE001 - durable retry state
        with session_scope() as session:
            row = session.get(BackupSyncDelivery, delivery_row_id)
            if row is not None:
                row.status = "failed"
                row.response_status = response_status
                row.response_excerpt = (response_excerpt or "")[:1000] or None
                row.error = str(exc)[:2000]
            destination = session.get(BackupDestination, destination_id)
            if destination is not None:
                destination.last_used_at = datetime.now(UTC)
                destination.health = {
                    "status": "unavailable",
                    "detail": str(exc)[:500],
                    "checked_at": datetime.now(UTC).isoformat(),
                }
        raise


def retry_delivery(delivery_id: int) -> dict:
    with session_scope() as session:
        row = session.get(BackupSyncDelivery, delivery_id)
        if row is None:
            raise LookupError("backup sync delivery not found")
        if row.destination_id is None:
            raise ValueError("backup destination authorization was revoked")
        backup_name, destination_id = row.backup_name, row.destination_id
    return deliver_backup(backup_name, destination_id)
