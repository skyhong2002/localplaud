"""Local AutoFlow matching, idempotent execution, and audit history."""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select

from .db.models import (
    AutomationExport,
    AutomationRule,
    AutomationRun,
    ExecutionProfile,
    Folder,
    NoteTemplate,
    Notification,
    PlaudFile,
    RecordingRuleProfileAssignment,
    StageName,
    StageRun,
    StageStatus,
    Tag,
)
from .db.session import session_scope
from .error_redaction import sanitize_error

# Early-transcript rules read only the opening of a recording, mirroring the
# audited Plaud behavior of matching how a conversation starts rather than
# scanning entire transcripts.
EARLY_TRANSCRIPT_WINDOW_CHARS = 4000


def early_transcript_text(recording: PlaudFile) -> str | None:
    """Opening text of the provenance-correct canonical transcript.

    Returns ``None`` while no transcript satisfying the configured artifact
    mode exists, so a transcript-keyword rule stays pending (no run row is
    recorded) and can still match after transcription completes. Plaud-only
    imports never satisfy independent mode, matching search/export semantics.
    """
    from .config import get_settings
    from .worker.pipeline import _select_raw_transcript

    raw = _select_raw_transcript(recording, get_settings())
    if raw is None:
        return None
    revision = recording.corrected_transcript_for_source(raw.source)
    segments = (revision.segments if revision is not None else raw.segments) or []
    parts: list[str] = []
    total = 0
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        parts.append(text)
        total += len(text) + 1
        if total >= EARLY_TRANSCRIPT_WINDOW_CHARS:
            break
    if not parts:
        return None
    return "\n".join(parts)[:EARLY_TRANSCRIPT_WINDOW_CHARS]


def rule_sentence(
    rule: AutomationRule | dict,
    *,
    translate: Callable[[str], str] | None = None,
) -> str:
    t = translate or (lambda value: value)
    trigger = rule.trigger if isinstance(rule, AutomationRule) else rule.get("trigger", {})
    actions = rule.actions if isinstance(rule, AutomationRule) else rule.get("actions", {})
    conditions = []
    if trigger.get("origin"):
        origin_label = "Plaud" if trigger["origin"] == "plaud" else t("Local import")
        conditions.append(t("source is {value}").format(value=origin_label))
    if trigger.get("title_contains"):
        conditions.append(
            t("title contains “{value}”").format(value=trigger["title_contains"])
        )
    if trigger.get("transcript_contains"):
        conditions.append(
            t("early transcript contains “{value}”").format(
                value=trigger["transcript_contains"]
            )
        )
    if trigger.get("min_duration_minutes") is not None:
        conditions.append(
            t("duration is at least {value} min").format(
                value=trigger["min_duration_minutes"]
            )
        )
    if trigger.get("max_duration_minutes") is not None:
        conditions.append(
            t("duration is at most {value} min").format(
                value=trigger["max_duration_minutes"]
            )
        )
    if trigger.get("folder_id") is not None:
        conditions.append(t("folder is #{value}").format(value=trigger["folder_id"]))
    if trigger.get("tag_id") is not None:
        conditions.append(t("tag includes #{value}").format(value=trigger["tag_id"]))
    effects = []
    if actions.get("note_template_key"):
        effects.append(
            t("use {value} notes").format(value=actions["note_template_key"])
        )
    if actions.get("profile_id") is not None:
        effects.append(
            t("use execution profile #{value}").format(value=actions["profile_id"])
        )
    if actions.get("folder_id") is not None:
        effects.append(t("move to folder #{value}").format(value=actions["folder_id"]))
    if actions.get("add_tag_ids"):
        effects.append(
            t("add tags {value}").format(
                value=t(", ").join(f"#{value}" for value in actions["add_tag_ids"])
            )
        )
    if actions.get("export_formats"):
        effects.append(
            t("export {value}").format(
                value="/".join(value.upper() for value in actions["export_formats"])
            )
        )
    if actions.get("webhook_integration_ids"):
        effects.append(
            t("send webhooks {value}").format(
                value=t(", ").join(
                    f"#{value}" for value in actions["webhook_integration_ids"]
                )
            )
        )
    if actions.get("email_integration_ids"):
        effects.append(
            t("send email {value}").format(
                value=t(", ").join(
                    f"#{value}" for value in actions["email_integration_ids"]
                )
            )
        )
    notify = rule.notify if isinstance(rule, AutomationRule) else bool(rule.get("notify"))
    if notify:
        effects.append(t("notify you"))
    condition_text = t(" and ").join(conditions) if conditions else t("a recording arrives")
    effect_text = t(", ").join(effects) if effects else t("record the match")
    return t("When {conditions}, then {effects}.").format(
        conditions=condition_text,
        effects=effect_text,
    )


def match_rule(rule: AutomationRule, recording: PlaudFile) -> tuple[bool, list[str]]:
    trigger = rule.trigger or {}
    reasons: list[str] = []
    if origin := trigger.get("origin"):
        if recording.origin != origin:
            return False, []
        reasons.append(f"source={origin}")
    if keyword := str(trigger.get("title_contains") or "").strip():
        if keyword.casefold() not in recording.display_title.casefold():
            return False, []
        reasons.append(f'title contains "{keyword}"')
    duration_minutes = (recording.duration_ms or 0) / 60_000
    if trigger.get("min_duration_minutes") is not None:
        minimum = float(trigger["min_duration_minutes"])
        if duration_minutes < minimum:
            return False, []
        reasons.append(f"duration≥{minimum:g}m")
    if trigger.get("max_duration_minutes") is not None:
        maximum = float(trigger["max_duration_minutes"])
        if duration_minutes > maximum:
            return False, []
        reasons.append(f"duration≤{maximum:g}m")
    if trigger.get("folder_id") is not None:
        if recording.folder_id != int(trigger["folder_id"]):
            return False, []
        reasons.append(f"folder=#{trigger['folder_id']}")
    if trigger.get("tag_id") is not None:
        tag_ids = {tag.id for tag in recording.tags}
        if int(trigger["tag_id"]) not in tag_ids:
            return False, []
        reasons.append(f"tag=#{trigger['tag_id']}")
    # Transcript loading is the expensive condition, so it runs only after every
    # cheap metadata condition has already matched.
    if keyword := str(trigger.get("transcript_contains") or "").strip():
        early = early_transcript_text(recording)
        if early is None or keyword.casefold() not in early.casefold():
            return False, []
        reasons.append(f'early transcript contains "{keyword}"')
    return True, reasons or ["all recordings"]


def _mark_notes_stale(session, file_id: str) -> None:
    from .worker.knowledge_index import invalidate_generated_documents

    invalidate_generated_documents(session, file_id)
    for stage in (StageName.summarize, StageName.mind_map):
        run = session.scalar(
            select(StageRun).where(StageRun.file_id == file_id, StageRun.stage == stage)
        )
        if run is not None:
            run.status = StageStatus.pending
            run.detail = (run.detail or {}) | {
                "stale": True,
                "stale_generation": secrets.token_hex(16),
                "reason": "AutoFlow changed notes",
            }
            run.error = None


def _apply_actions(
    session, rule: AutomationRule, recording: PlaudFile, *, automation_run_id: int
) -> dict:
    actions = rule.actions or {}
    applied: dict = {}
    profile_resolution_changed = False
    if (
        actions.get("note_template_key")
        or actions.get("profile_id") is not None
        or actions.get("folder_id") is not None
        or actions.get("add_tag_ids")
    ):
        from .providers.service import lock_recording_membership_changes

        lock_recording_membership_changes(session, [recording.id])
        recording = session.get(PlaudFile, recording.id) or recording
    if key := actions.get("note_template_key"):
        recording.note_template_key = key
        _mark_notes_stale(session, recording.id)
        applied["note_template_key"] = key
        profile_resolution_changed = True
    if actions.get("profile_id") is not None:
        profile_id = int(actions["profile_id"])
        assignment = session.get(
            RecordingRuleProfileAssignment, (recording.id, rule.id)
        )
        if assignment is None:
            session.add(
                RecordingRuleProfileAssignment(
                    file_id=recording.id,
                    rule_id=rule.id,
                    rule_version=rule.version,
                    priority_snapshot=rule.priority,
                    profile_id=profile_id,
                    automation_run_id=automation_run_id,
                    rule_snapshot={
                        "name": rule.name,
                        "owner_type": rule.owner_type,
                        "owner_key": rule.owner_key,
                    },
                )
            )
        else:
            assignment.profile_id = profile_id
            assignment.rule_version = rule.version
            assignment.priority_snapshot = rule.priority
            assignment.automation_run_id = automation_run_id
            assignment.rule_snapshot = {
                "name": rule.name,
                "owner_type": rule.owner_type,
                "owner_key": rule.owner_key,
            }
        applied["profile_id"] = profile_id
        profile_resolution_changed = True
    if actions.get("folder_id") is not None:
        recording.folder_id = int(actions["folder_id"])
        applied["folder_id"] = recording.folder_id
        profile_resolution_changed = True
    if actions.get("add_tag_ids"):
        existing = {tag.id for tag in recording.tags}
        tags = list(
            session.scalars(select(Tag).where(Tag.id.in_(actions["add_tag_ids"])))
        )
        recording.tags.extend(tag for tag in tags if tag.id not in existing)
        applied["add_tag_ids"] = [tag.id for tag in tags]
    if profile_resolution_changed:
        from .worker.knowledge_index import sync_file_knowledge_documents

        sync_file_knowledge_documents(session, recording.id)
    return applied


def evaluate_recording(file_id: str) -> list[dict]:
    """Apply every matching enabled rule once per rule version.

    Lower numeric priority wins because it executes last and may intentionally
    override a broader lower-priority rule.
    """
    results: list[dict] = []
    with session_scope() as session:
        rule_ids = list(
            session.scalars(
                select(AutomationRule.id)
                .where(AutomationRule.enabled.is_(True))
                .order_by(AutomationRule.priority.desc(), AutomationRule.id)
            )
        )
    for rule_id in rule_ids:
        run: AutomationRun | None = None
        downstream_run_id: int | None = None
        notification_requested = False
        with session_scope() as session:
            rule = session.get(AutomationRule, rule_id)
            recording = session.get(PlaudFile, file_id)
            if rule is None or recording is None or not rule.enabled:
                continue
            existing = session.scalar(
                select(AutomationRun.id).where(
                    AutomationRun.rule_id == rule.id,
                    AutomationRun.rule_version == rule.version,
                    AutomationRun.file_id == file_id,
                )
            )
            if existing is not None:
                continue
            matched, reasons = match_rule(rule, recording)
            if not matched:
                continue
            try:
                from .integrations import webhook_snapshots

                webhook_requested = webhook_snapshots(
                    session,
                    list((rule.actions or {}).get("webhook_integration_ids", [])),
                    require_enabled=False,
                )
                from .email_integrations import email_snapshots

                email_requested = email_snapshots(
                    session,
                    list((rule.actions or {}).get("email_integration_ids", [])),
                    require_enabled=False,
                )
                run = AutomationRun(
                    rule_id=rule.id,
                    rule_version=rule.version,
                    file_id=file_id,
                    status="running",
                    matched=True,
                    detail={
                        "rule_name": rule.name,
                        "reasons": reasons,
                        "notification_requested": rule.notify,
                        "export_requested": list((rule.actions or {}).get("export_formats", [])),
                        "webhook_requested": webhook_requested,
                        "email_requested": email_requested,
                    },
                )
                session.add(run)
                session.flush()
                with session.begin_nested():
                    applied = _apply_actions(
                        session, rule, recording, automation_run_id=run.id
                    )
                run.status = "completed"
                run.detail = (run.detail or {}) | {"applied": applied}
                downstream_run_id = run.id
                notification_requested = rule.notify
                results.append({"rule_id": rule.id, "status": "completed", "applied": applied})
            except Exception as exc:  # noqa: BLE001
                error = sanitize_error(exc)
                if run is not None:
                    run.status = "failed"
                    run.detail = {"rule_name": rule.name, "reasons": reasons}
                    run.error = error
                else:
                    session.add(
                        AutomationRun(
                            rule_id=rule.id,
                            rule_version=rule.version,
                            file_id=file_id,
                            status="failed",
                            matched=True,
                            detail={"rule_name": rule.name, "reasons": reasons},
                            error=error,
                        )
                    )
                results.append({"rule_id": rule.id, "status": "failed", "error": error})
        if downstream_run_id is not None and notification_requested:
            try:
                notification = deliver_local_notification(downstream_run_id)
                results[-1]["notification"] = notification
            except Exception as exc:  # noqa: BLE001 - actions remain committed
                _record_notification_failure(downstream_run_id, exc)
                results[-1]["notification"] = {
                    "status": "failed",
                    "error": sanitize_error(exc),
                }
        if downstream_run_id is not None:
            exports = []
            for fmt in _requested_export_formats(downstream_run_id):
                try:
                    exports.append(deliver_automation_export(downstream_run_id, fmt))
                except Exception as exc:  # noqa: BLE001 - actions remain committed
                    exports.append(
                        {"format": fmt, "status": "failed", "error": sanitize_error(exc)}
                    )
            if exports:
                results[-1]["exports"] = exports
            webhooks = []
            for snapshot in _requested_webhooks(downstream_run_id):
                try:
                    from .integrations import deliver_webhook

                    webhooks.append(deliver_webhook(downstream_run_id, snapshot))
                except Exception as exc:  # noqa: BLE001 - actions remain committed
                    webhooks.append({"status": "failed", "error": sanitize_error(exc)})
            if webhooks:
                results[-1]["webhooks"] = webhooks
            emails = []
            for snapshot in _requested_emails(downstream_run_id):
                try:
                    from .email_integrations import deliver_email

                    emails.append(deliver_email(downstream_run_id, snapshot))
                except Exception as exc:  # noqa: BLE001 - actions remain committed
                    emails.append({"status": "failed", "error": sanitize_error(exc)})
            if emails:
                results[-1]["emails"] = emails
    return results


def _requested_export_formats(run_id: int) -> list[str]:
    with session_scope() as session:
        run = session.get(AutomationRun, run_id)
        if run is None:
            return []
        return [
            value
            for value in (run.detail or {}).get("export_requested", [])
            if value in {"txt", "srt", "vtt"}
        ]


def _requested_webhooks(run_id: int) -> list[dict]:
    with session_scope() as session:
        run = session.get(AutomationRun, run_id)
        if run is None:
            return []
        return [
            dict(value)
            for value in (run.detail or {}).get("webhook_requested", [])
            if isinstance(value, dict) and value.get("id") is not None
        ]


def _requested_emails(run_id: int) -> list[dict]:
    with session_scope() as session:
        run = session.get(AutomationRun, run_id)
        if run is None:
            return []
        return [
            dict(value)
            for value in (run.detail or {}).get("email_requested", [])
            if isinstance(value, dict) and value.get("id") is not None
        ]


def _export_path(run: AutomationRun, fmt: str) -> Path:
    from .config import get_settings

    return (
        get_settings().poller.download_dir
        / run.file_id
        / "autoflow"
        / f"run-{run.id}"
        / f"transcript.{fmt}"
    )


def deliver_automation_export(run_id: int, fmt: str) -> dict:
    """Create or retry one canonical transcript export without affecting rule actions."""
    if fmt not in {"txt", "srt", "vtt"}:
        raise ValueError("unsupported automation export format")
    with session_scope() as session:
        run = session.get(AutomationRun, run_id)
        if run is None or run.status != "completed":
            raise ValueError("completed automation run not found")
        if fmt not in (run.detail or {}).get("export_requested", []):
            raise ValueError("export format was not requested by this run")
        row = session.scalar(
            select(AutomationExport).where(
                AutomationExport.automation_run_id == run_id,
                AutomationExport.format == fmt,
            )
        )
        if row is None:
            row = AutomationExport(
                automation_run_id=run_id,
                file_id=run.file_id,
                format=fmt,
            )
            session.add(row)
            session.flush()
        path = _export_path(run, fmt)
        if row.status == "completed" and path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest == row.sha256:
                return {"id": row.id, "format": fmt, "status": "completed"}
        row.status = "running"
        row.error = None
        export_id = row.id
        file_id = run.file_id

    try:
        from .export_formats import recording_data, render_transcript_data

        snapshot = recording_data(file_id)
        content, _media_type = render_transcript_data(snapshot, fmt)
        provenance = snapshot["transcript_provenance"]
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".transcript-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        digest = hashlib.sha256(content).hexdigest()
        with session_scope() as session:
            row = session.get(AutomationExport, export_id)
            if row is None:
                raise ValueError("automation export was deleted during delivery")
            row.status = "completed"
            row.path = str(path)
            row.sha256 = digest
            row.size_bytes = len(content)
            row.provenance = provenance
            row.error = None
        return {"id": export_id, "format": fmt, "status": "completed"}
    except Exception as exc:  # noqa: BLE001 - durable failure is independently retryable
        error = sanitize_error(exc)
        with session_scope() as session:
            row = session.get(AutomationExport, export_id)
            if row is not None:
                row.status = "failed"
                row.error = error
        return {"id": export_id, "format": fmt, "status": "failed", "error": error}


def deliver_local_notification(run_id: int) -> dict:
    """Create exactly one inbox item for a completed run."""
    with session_scope() as session:
        existing = session.scalar(
            select(Notification).where(Notification.automation_run_id == run_id)
        )
        if existing is not None:
            return {"status": "delivered", "notification_id": existing.id}
        run = session.get(AutomationRun, run_id)
        if run is None or run.status != "completed":
            raise ValueError("completed automation run not found")
        rule = session.get(AutomationRule, run.rule_id)
        recording = session.get(PlaudFile, run.file_id)
        requested = bool((run.detail or {}).get("notification_requested"))
        if rule is None or not requested:
            raise ValueError("notification is not enabled for this run")
        rule_name = str((run.detail or {}).get("rule_name") or rule.name)
        reasons = list((run.detail or {}).get("reasons", []))
        row = Notification(
            automation_run_id=run.id,
            file_id=run.file_id,
            title=f"AutoFlow completed: {rule_name}",
            body=(recording.display_title if recording else run.file_id),
            detail={
                "rule_id": rule.id,
                "rule_name": rule_name,
                "rule_version": run.rule_version,
                "recording_title": recording.display_title if recording else run.file_id,
                "reasons": reasons,
                "applied": (run.detail or {}).get("applied", {}),
            },
        )
        session.add(row)
        session.flush()
        run.detail = (run.detail or {}) | {
            "notification": {"status": "delivered", "notification_id": row.id}
        }
        return {"status": "delivered", "notification_id": row.id}


def _record_notification_failure(run_id: int, exc: Exception) -> None:
    try:
        with session_scope() as session:
            run = session.get(AutomationRun, run_id)
            if run is not None:
                run.detail = (run.detail or {}) | {
                    "notification": {
                        "status": "failed",
                        "error": sanitize_error(exc, max_length=1000),
                    }
                }
    except Exception:  # noqa: BLE001 - notification metadata must never affect actions
        pass


def evaluate_library(limit: int | None = None) -> int:
    with session_scope() as session:
        stmt = select(PlaudFile.id).order_by(PlaudFile.start_time_ms.desc())
        if limit:
            stmt = stmt.limit(limit)
        file_ids = list(session.scalars(stmt))
    return sum(bool(evaluate_recording(file_id)) for file_id in file_ids)


def validate_rule_references(trigger: dict, actions: dict) -> None:
    with session_scope() as session:
        if trigger.get("folder_id") is not None and session.get(Folder, int(trigger["folder_id"])) is None:
            raise ValueError("trigger folder not found")
        if trigger.get("tag_id") is not None and session.get(Tag, int(trigger["tag_id"])) is None:
            raise ValueError("trigger tag not found")
        if actions.get("folder_id") is not None and session.get(Folder, int(actions["folder_id"])) is None:
            raise ValueError("action folder not found")
        for tag_id in actions.get("add_tag_ids", []):
            if session.get(Tag, int(tag_id)) is None:
                raise ValueError(f"action tag #{tag_id} not found")
        if actions.get("profile_id") is not None and session.get(ExecutionProfile, int(actions["profile_id"])) is None:
            raise ValueError("execution profile not found")
        key = actions.get("note_template_key")
        if key and key != "auto" and session.scalar(
            select(NoteTemplate.id).where(NoteTemplate.key == key, NoteTemplate.is_active.is_(True))
        ) is None:
            raise ValueError("note template not found")
        from .integrations import webhook_snapshots

        webhook_snapshots(session, list(actions.get("webhook_integration_ids", [])))
        from .email_integrations import email_snapshots

        email_snapshots(session, list(actions.get("email_integration_ids", [])))
