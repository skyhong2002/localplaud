"""Authorized SMTP destinations and durable AutoFlow email delivery."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import smtplib
import socket
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parseaddr

from sqlalchemy import select

from .db.models import (
    AutomationEmailDelivery,
    AutomationRule,
    AutomationRun,
    EmailIntegration,
    PlaudFile,
)
from .db.session import session_scope
from .export_formats import recording_data

EMAIL_SCOPES = {"metadata", "transcript", "notes"}
MAX_EMAIL_BYTES = 5 * 1024 * 1024
_ENV_REF = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _valid_address(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise ValueError("email addresses must not contain line breaks")
    _name, address = parseaddr(value)
    if address != value.strip() or not _EMAIL.fullmatch(address):
        raise ValueError(f"invalid email address: {value}")
    return address


def _validate_host(host: str, port: int, *, allow_private: bool) -> None:
    if not host or any(character.isspace() for character in host):
        raise ValueError("SMTP host is invalid")
    try:
        addresses = socket.getaddrinfo(host, port)
    except socket.gaierror as exc:
        raise ValueError("SMTP hostname could not be resolved") from exc
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
        if unsafe and not allow_private:
            raise ValueError("SMTP host resolves to a private or non-routable address")


def _password(password_ref: str | None) -> str | None:
    if not password_ref:
        return None
    if not _ENV_REF.fullmatch(password_ref):
        raise ValueError("SMTP password reference must use env:VARIABLE")
    name = password_ref.removeprefix("env:")
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"SMTP password environment variable is missing: {name}")
    return value


def serialize_email_integration(row: EmailIntegration) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "smtp_host": row.smtp_host,
        "smtp_port": row.smtp_port,
        "security": row.security,
        "allow_insecure_private": row.allow_insecure_private,
        "username": row.username,
        "password_ref": row.password_ref,
        "from_address": row.from_address,
        "to_addresses": list(row.to_addresses or []),
        "subject_prefix": row.subject_prefix,
        "scopes": list(row.scopes or ["metadata"]),
        "enabled": row.enabled,
        "health": row.health or {},
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def list_email_integrations(session) -> list[dict]:
    return [
        serialize_email_integration(row)
        for row in session.scalars(select(EmailIntegration).order_by(EmailIntegration.name))
    ]


def save_email_integration(session, data: dict, integration_id: int | None = None) -> dict:
    scopes = list(dict.fromkeys(data.get("scopes") or ["metadata"]))
    if not scopes or not set(scopes) <= EMAIL_SCOPES:
        raise ValueError("email scopes must use metadata, transcript, or notes")
    security = data.get("security", "starttls")
    if security not in {"starttls", "tls", "plain"}:
        raise ValueError("SMTP security must be starttls, tls, or plain")
    allow_insecure = bool(data.get("allow_insecure_private"))
    if security == "plain" and not allow_insecure:
        raise ValueError("plain SMTP requires explicit insecure/private allowance")
    port = int(data.get("smtp_port", 587))
    if not 1 <= port <= 65535:
        raise ValueError("SMTP port must be between 1 and 65535")
    _validate_host(data["smtp_host"], port, allow_private=allow_insecure)
    password_ref = data.get("password_ref")
    if password_ref and not _ENV_REF.fullmatch(password_ref):
        raise ValueError("SMTP password reference must use env:VARIABLE")
    username = (data.get("username") or "").strip() or None
    if password_ref and not username:
        raise ValueError("SMTP username is required when a password reference is set")
    from_address = _valid_address(data["from_address"])
    to_addresses = [_valid_address(value) for value in data["to_addresses"]]
    if not to_addresses or len(to_addresses) > 20:
        raise ValueError("SMTP integration requires 1 to 20 recipients")
    prefix = data.get("subject_prefix", "[localplaud]").strip()
    if "\r" in prefix or "\n" in prefix:
        raise ValueError("email subject prefix must not contain line breaks")
    row = session.get(EmailIntegration, integration_id) if integration_id else None
    if integration_id and row is None:
        raise LookupError("email integration not found")
    if row is None:
        row = EmailIntegration(name=data["name"], smtp_host=data["smtp_host"])
        session.add(row)
    row.name = data["name"].strip()
    row.smtp_host = data["smtp_host"]
    row.smtp_port = port
    row.security = security
    row.allow_insecure_private = allow_insecure
    row.username = username
    row.password_ref = password_ref
    row.from_address = from_address
    row.to_addresses = to_addresses
    row.subject_prefix = prefix
    row.scopes = scopes
    row.enabled = bool(data.get("enabled", True))
    session.flush()
    return serialize_email_integration(row)


def email_snapshot(row: EmailIntegration) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "smtp_host": row.smtp_host,
        "smtp_port": row.smtp_port,
        "security": row.security,
        "allow_insecure_private": row.allow_insecure_private,
        "username": row.username,
        "password_ref": row.password_ref,
        "from_address": row.from_address,
        "to_addresses": list(row.to_addresses or []),
        "subject_prefix": row.subject_prefix,
        "scopes": list(row.scopes or ["metadata"]),
        "enabled": row.enabled,
    }


def email_snapshots(session, integration_ids: list[int], *, require_enabled: bool = True) -> list[dict]:
    snapshots = []
    for integration_id in dict.fromkeys(integration_ids):
        row = session.get(EmailIntegration, int(integration_id))
        if row is None:
            raise ValueError(f"email integration #{integration_id} not found")
        if require_enabled and not row.enabled:
            raise ValueError(f"email integration #{integration_id} is disabled")
        snapshots.append(email_snapshot(row))
    return snapshots


def delete_email_integration(session, integration_id: int) -> None:
    row = session.get(EmailIntegration, integration_id)
    if row is None:
        raise LookupError("email integration not found")
    for rule in session.scalars(select(AutomationRule)):
        if integration_id in (rule.actions or {}).get("email_integration_ids", []):
            raise ValueError("email integration is used by an AutoFlow rule")
    for delivery in session.scalars(
        select(AutomationEmailDelivery).where(
            AutomationEmailDelivery.integration_id == integration_id
        )
    ):
        delivery.integration_id = None
    session.delete(row)


def build_email(file_id: str, snapshot: dict, idempotency_key: str, message_id: str) -> EmailMessage:
    with session_scope() as session:
        recording = session.get(PlaudFile, file_id)
        if recording is None:
            raise ValueError("recording not found")
        title = recording.display_title
        lines = [
            title,
            "",
            f"Recording ID: {recording.id}",
            f"Source: {recording.origin}",
            f"Duration: {recording.duration_ms or 0} ms",
            f"Recorded at: {recording.start_time_ms or 'unknown'}",
            f"Folder: {recording.folder.name if recording.folder else 'none'}",
            "Tags: " + (", ".join(tag.name for tag in recording.tags) or "none"),
        ]
    scopes = set(snapshot.get("scopes") or ["metadata"])
    data = recording_data(file_id) if scopes & {"transcript", "notes"} else None
    if "transcript" in scopes and data is not None:
        lines += ["", "Transcript", "----------"]
        names = data["speaker_names"]
        for segment in data["segments"]:
            speaker = segment.get("speaker")
            label = names.get(speaker, speaker) if speaker else None
            stamp = float(segment.get("start") or 0)
            prefix = f"[{int(stamp)//60:02d}:{int(stamp)%60:02d}] "
            lines.append(prefix + (f"{label}: " if label else "") + str(segment.get("text") or ""))
    if "notes" in scopes and data is not None:
        for note in data["notes"]:
            lines += ["", str(note["title"]), "-" * len(str(note["title"])), str(note["content"])]
    message = EmailMessage()
    message["From"] = snapshot["from_address"]
    message["To"] = ", ".join(snapshot["to_addresses"])
    message["Subject"] = f"{snapshot.get('subject_prefix') or '[localplaud]'} {title}"
    message["Message-ID"] = message_id
    message["X-Localplaud-Delivery-Id"] = idempotency_key
    message.set_content("\n".join(lines))
    if len(message.as_bytes()) > MAX_EMAIL_BYTES:
        raise ValueError("email payload exceeds 5 MiB; reduce the integration scope")
    return message


def _send_email(snapshot: dict, message: EmailMessage) -> None:
    host, port = snapshot["smtp_host"], int(snapshot["smtp_port"])
    allow_private = bool(snapshot.get("allow_insecure_private"))
    _validate_host(host, port, allow_private=allow_private)
    security = snapshot["security"]
    context = ssl.create_default_context()
    smtp_class = smtplib.SMTP_SSL if security == "tls" else smtplib.SMTP
    kwargs = {"host": host, "port": port, "timeout": 15}
    if security == "tls":
        kwargs["context"] = context
    with smtp_class(**kwargs) as client:
        client.ehlo()
        if security == "starttls":
            client.starttls(context=context)
            client.ehlo()
        if password := _password(snapshot.get("password_ref")):
            client.login(snapshot["username"], password)
        refused = client.send_message(message)
        if refused:
            raise RuntimeError(f"SMTP refused {len(refused)} recipient(s)")


def test_email_integration(session, integration_id: int) -> dict:
    row = session.get(EmailIntegration, integration_id)
    if row is None:
        raise LookupError("email integration not found")
    snapshot = email_snapshot(row)
    message = EmailMessage()
    message["From"] = snapshot["from_address"]
    message["To"] = ", ".join(snapshot["to_addresses"])
    message["Subject"] = f"{snapshot['subject_prefix']} localplaud email test"
    message["Message-ID"] = f"<email-test-{integration_id}@localplaud.local>"
    message["X-Localplaud-Delivery-Id"] = f"email-test-{integration_id}"
    message.set_content("This is a localplaud SMTP integration test. No recording data is included.")
    checked_at = datetime.now(UTC).isoformat()
    try:
        _send_email(snapshot, message)
        row.health = {"status": "healthy", "detail": "SMTP test accepted", "checked_at": checked_at}
    except Exception as exc:  # noqa: BLE001 - health is persisted
        row.health = {"status": "unavailable", "detail": str(exc), "checked_at": checked_at}
    session.flush()
    return row.health


def deliver_email(run_id: int, snapshot: dict) -> dict:
    integration_id = int(snapshot["id"])
    idempotency_key = f"autoflow-run-{run_id}-email-{integration_id}"
    message_id = f"<{idempotency_key}@localplaud.local>"
    with session_scope() as session:
        run = session.get(AutomationRun, run_id)
        if run is None or run.status != "completed":
            raise ValueError("completed automation run not found")
        row = session.scalar(
            select(AutomationEmailDelivery).where(
                AutomationEmailDelivery.idempotency_key == idempotency_key
            )
        )
        if row is None:
            row = AutomationEmailDelivery(
                automation_run_id=run_id,
                integration_id=integration_id,
                file_id=run.file_id,
                idempotency_key=idempotency_key,
                message_id=message_id,
                integration_snapshot=snapshot,
            )
            session.add(row)
            session.flush()
        if row.status == "completed":
            return {"id": row.id, "status": "completed", "message_id": row.message_id}
        row.status = "running"
        row.attempt_count += 1
        row.error = None
        delivery_id, file_id = row.id, run.file_id
    payload_sha256 = None
    try:
        if not snapshot.get("enabled", True):
            raise ValueError("email integration was disabled before this run")
        message = build_email(file_id, snapshot, idempotency_key, message_id)
        payload_sha256 = hashlib.sha256(message.as_bytes()).hexdigest()
        _send_email(snapshot, message)
        with session_scope() as session:
            row = session.get(AutomationEmailDelivery, delivery_id)
            row.status = "completed"
            row.payload_sha256 = payload_sha256
            row.error = None
            integration = session.get(EmailIntegration, integration_id)
            if integration is not None:
                integration.last_used_at = datetime.now(UTC)
                integration.health = {
                    "status": "healthy",
                    "detail": "last message accepted by SMTP",
                    "checked_at": datetime.now(UTC).isoformat(),
                }
        return {"id": delivery_id, "status": "completed", "message_id": message_id}
    except Exception as exc:  # noqa: BLE001 - durable failure remains retryable
        with session_scope() as session:
            row = session.get(AutomationEmailDelivery, delivery_id)
            if row is not None:
                row.status = "failed"
                row.payload_sha256 = payload_sha256
                row.error = str(exc)[:2000]
            integration = session.get(EmailIntegration, integration_id)
            if integration is not None:
                integration.last_used_at = datetime.now(UTC)
                integration.health = {
                    "status": "unavailable",
                    "detail": str(exc)[:1000],
                    "checked_at": datetime.now(UTC).isoformat(),
                }
        return {"id": delivery_id, "status": "failed", "error": str(exc)}
