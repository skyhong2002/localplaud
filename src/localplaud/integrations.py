"""Authorized outbound integrations and durable webhook delivery."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from .db.models import (
    AutomationRule,
    AutomationRun,
    AutomationWebhookDelivery,
    PlaudFile,
    WebhookIntegration,
)
from .db.session import session_scope
from .export_formats import recording_data

WEBHOOK_SCOPES = {"metadata", "transcript", "notes"}
MAX_WEBHOOK_PAYLOAD_BYTES = 5 * 1024 * 1024
MAX_WEBHOOK_RESPONSE_BYTES = 64 * 1024
_ENV_REF = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")


def serialize_integration(row: WebhookIntegration) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "secret_ref": row.secret_ref,
        "scopes": list(row.scopes or ["metadata"]),
        "enabled": row.enabled,
        "allow_private_network": row.allow_private_network,
        "health": row.health or {},
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def list_webhook_integrations(session) -> list[dict]:
    return [
        serialize_integration(row)
        for row in session.scalars(select(WebhookIntegration).order_by(WebhookIntegration.name))
    ]


def save_webhook_integration(session, data: dict, integration_id: int | None = None) -> dict:
    scopes = list(dict.fromkeys(data.get("scopes") or ["metadata"]))
    if not scopes or not set(scopes) <= WEBHOOK_SCOPES:
        raise ValueError("webhook scopes must use metadata, transcript, or notes")
    secret_ref = data.get("secret_ref")
    if secret_ref and not _ENV_REF.fullmatch(secret_ref):
        raise ValueError("webhook secret reference must use env:VARIABLE")
    allow_private = bool(data.get("allow_private_network"))
    validate_webhook_url(data["url"], allow_private_network=allow_private)
    row = session.get(WebhookIntegration, integration_id) if integration_id else None
    if integration_id and row is None:
        raise LookupError("webhook integration not found")
    if row is None:
        row = WebhookIntegration(name=data["name"], url=data["url"])
        session.add(row)
    row.name = data["name"].strip()
    row.url = data["url"]
    row.secret_ref = secret_ref
    row.scopes = scopes
    row.enabled = bool(data.get("enabled", True))
    row.allow_private_network = allow_private
    session.flush()
    return serialize_integration(row)


def delete_webhook_integration(session, integration_id: int) -> None:
    row = session.get(WebhookIntegration, integration_id)
    if row is None:
        raise LookupError("webhook integration not found")
    for rule in session.scalars(select(AutomationRule)):
        if integration_id in (rule.actions or {}).get("webhook_integration_ids", []):
            raise ValueError("webhook integration is used by an AutoFlow rule")
    for delivery in session.scalars(
        select(AutomationWebhookDelivery).where(
            AutomationWebhookDelivery.integration_id == integration_id
        )
    ):
        delivery.integration_id = None
    session.delete(row)


def test_webhook_integration(session, integration_id: int) -> dict:
    row = session.get(WebhookIntegration, integration_id)
    if row is None:
        raise LookupError("webhook integration not found")
    snapshot = integration_snapshot(row)
    checked_at = datetime.now(UTC).isoformat()
    body = json.dumps(
        {"type": "localplaud.webhook.test", "version": 1}, separators=(",", ":")
    ).encode()
    try:
        status_code, _excerpt = _post_webhook(
            snapshot, body, f"webhook-test-{integration_id}"
        )
        if not 200 <= status_code < 300:
            raise RuntimeError(f"webhook returned HTTP {status_code}")
        row.health = {
            "status": "healthy",
            "detail": f"test HTTP {status_code}",
            "checked_at": checked_at,
        }
    except Exception as exc:  # noqa: BLE001 - health is persisted, not raised
        row.health = {"status": "unavailable", "detail": str(exc), "checked_at": checked_at}
    session.flush()
    return row.health


def validate_webhook_url(url: str, *, allow_private_network: bool) -> None:
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "webhook URL must not contain credentials, query parameters, or a fragment"
        )
    if not parsed.hostname or parsed.scheme not in {"http", "https"}:
        raise ValueError("webhook URL must be an absolute HTTP(S) URL")
    if parsed.scheme != "https" and not allow_private_network:
        raise ValueError("public webhooks require HTTPS")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError("webhook hostname could not be resolved") from exc
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
            raise ValueError("webhook resolves to a private or non-routable address")


def integration_snapshot(row: WebhookIntegration) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "secret_ref": row.secret_ref,
        "scopes": list(row.scopes or ["metadata"]),
        "allow_private_network": row.allow_private_network,
        "enabled": row.enabled,
    }


def webhook_snapshots(
    session, integration_ids: list[int], *, require_enabled: bool = True
) -> list[dict]:
    snapshots = []
    for integration_id in dict.fromkeys(integration_ids):
        row = session.get(WebhookIntegration, int(integration_id))
        if row is None:
            raise ValueError(f"webhook integration #{integration_id} not found")
        if require_enabled and not row.enabled:
            raise ValueError(f"webhook integration #{integration_id} is disabled")
        snapshots.append(integration_snapshot(row))
    return snapshots


def _secret_value(secret_ref: str | None) -> str | None:
    if not secret_ref:
        return None
    if not _ENV_REF.fullmatch(secret_ref):
        raise ValueError("webhook secret reference must use env:VARIABLE")
    name = secret_ref.removeprefix("env:")
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"webhook secret environment variable is missing: {name}")
    return value


def build_webhook_payload(file_id: str, snapshot: dict, idempotency_key: str) -> bytes:
    with session_scope() as session:
        recording = session.get(PlaudFile, file_id)
        if recording is None:
            raise ValueError("recording not found")
        payload: dict = {
            "type": "localplaud.autoflow.completed",
            "version": 1,
            "idempotency_key": idempotency_key,
            "recording": {
                "id": recording.id,
                "title": recording.display_title,
                "origin": recording.origin,
                "duration_ms": recording.duration_ms,
                "start_time_ms": recording.start_time_ms,
                "folder": recording.folder.name if recording.folder else None,
                "tags": [tag.name for tag in recording.tags],
            },
        }
    scopes = set(snapshot.get("scopes") or ["metadata"])
    data = recording_data(file_id) if scopes & {"transcript", "notes"} else None
    if "transcript" in scopes and data is not None:
        names = data["speaker_names"]
        payload["transcript"] = {
            "provenance": data["transcript_provenance"],
            "segments": [
                dict(segment)
                | {
                    "speaker_name": names.get(segment.get("speaker"), segment.get("speaker"))
                    if segment.get("speaker")
                    else None
                }
                for segment in data["segments"]
            ],
        }
    if "notes" in scopes and data is not None:
        payload["notes"] = data["notes"]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_WEBHOOK_PAYLOAD_BYTES:
        raise ValueError("webhook payload exceeds 5 MiB; reduce the integration scope")
    return encoded


def _post_webhook(snapshot: dict, body: bytes, idempotency_key: str) -> tuple[int, str]:
    validate_webhook_url(
        snapshot["url"], allow_private_network=bool(snapshot.get("allow_private_network"))
    )
    headers = {
        "content-type": "application/json",
        "user-agent": "localplaud-webhook/1",
        "x-localplaud-delivery-id": idempotency_key,
    }
    if token := _secret_value(snapshot.get("secret_ref")):
        headers["authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        with client.stream("POST", snapshot["url"], content=body, headers=headers) as response:
            captured = bytearray()
            for chunk in response.iter_bytes():
                remaining = MAX_WEBHOOK_RESPONSE_BYTES - len(captured)
                if remaining <= 0:
                    break
                captured.extend(chunk[:remaining])
                if len(captured) >= MAX_WEBHOOK_RESPONSE_BYTES:
                    break
            excerpt = bytes(captured).decode("utf-8", errors="replace")
            return response.status_code, excerpt


def deliver_webhook(run_id: int, snapshot: dict) -> dict:
    integration_id = int(snapshot["id"])
    idempotency_key = f"autoflow-run-{run_id}-webhook-{integration_id}"
    with session_scope() as session:
        run = session.get(AutomationRun, run_id)
        if run is None or run.status != "completed":
            raise ValueError("completed automation run not found")
        row = session.scalar(
            select(AutomationWebhookDelivery).where(
                AutomationWebhookDelivery.idempotency_key == idempotency_key
            )
        )
        if row is None:
            row = AutomationWebhookDelivery(
                automation_run_id=run_id,
                integration_id=integration_id,
                file_id=run.file_id,
                idempotency_key=idempotency_key,
                integration_snapshot=snapshot,
            )
            session.add(row)
            session.flush()
        if row.status == "completed":
            return {"id": row.id, "status": "completed", "response_status": row.response_status}
        row.status = "running"
        row.attempt_count += 1
        row.error = None
        delivery_id, file_id = row.id, run.file_id

    payload_sha256 = None
    response_status = None
    response_excerpt = None
    try:
        if not snapshot.get("enabled", True):
            raise ValueError("webhook integration was disabled before this run")
        body = build_webhook_payload(file_id, snapshot, idempotency_key)
        payload_sha256 = hashlib.sha256(body).hexdigest()
        response_status, response_excerpt = _post_webhook(snapshot, body, idempotency_key)
        if not 200 <= response_status < 300:
            raise RuntimeError(f"webhook returned HTTP {response_status}")
        with session_scope() as session:
            row = session.get(AutomationWebhookDelivery, delivery_id)
            row.status = "completed"
            row.payload_sha256 = payload_sha256
            row.response_status = response_status
            row.response_excerpt = response_excerpt[:1000]
            row.error = None
            integration = session.get(WebhookIntegration, integration_id)
            if integration is not None:
                integration.last_used_at = datetime.now(UTC)
                integration.health = {
                    "status": "healthy",
                    "detail": f"last delivery HTTP {response_status}",
                    "checked_at": datetime.now(UTC).isoformat(),
                }
        return {
            "id": delivery_id,
            "status": "completed",
            "response_status": response_status,
        }
    except Exception as exc:  # noqa: BLE001 - durable failure remains independently retryable
        with session_scope() as session:
            row = session.get(AutomationWebhookDelivery, delivery_id)
            if row is not None:
                row.status = "failed"
                row.payload_sha256 = payload_sha256
                row.response_status = response_status
                row.response_excerpt = (response_excerpt or "")[:1000] or None
                row.error = str(exc)[:2000]
            integration = session.get(WebhookIntegration, integration_id)
            if integration is not None:
                integration.last_used_at = datetime.now(UTC)
                integration.health = {
                    "status": "unavailable",
                    "detail": str(exc)[:1000],
                    "checked_at": datetime.now(UTC).isoformat(),
                }
        return {"id": delivery_id, "status": "failed", "error": str(exc)}
