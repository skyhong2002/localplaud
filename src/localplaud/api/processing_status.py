"""Read-only workspace overview based on recorded processing and health state."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from ..db.models import (
    ExecutionProfile,
    FileStatus,
    PlaudFile,
    RemoteWorker,
    StageRun,
    StageStatus,
)


def _check_state(health, now):
    checked_at = health.get("checked_at")
    if checked_at:
        try:
            age = now - datetime.fromisoformat(checked_at)
        except (ValueError, TypeError):
            checked_at = None
        else:
            if timedelta(0) <= age <= timedelta(minutes=5):
                return health.get("status", "unknown"), checked_at
    return "unknown", checked_at


def processing_overview(session, settings, *, plaud_connected):
    now = datetime.now(UTC)
    counts = dict(
        session.execute(
            select(PlaudFile.status, func.count())
            .where(PlaudFile.is_trash.is_(False))
            .group_by(PlaudFile.status)
        ).all()
    )
    failed_stages = select(StageRun.file_id).where(
        StageRun.status.in_((StageStatus.degraded, StageStatus.failed))
    )
    issue = session.execute(
        select(StageRun.file_id, StageRun.stage)
        .join(PlaudFile, PlaudFile.id == StageRun.file_id)
        .where(
            PlaudFile.is_trash.is_(False),
            StageRun.status.in_((StageStatus.degraded, StageStatus.failed)),
        )
        .order_by(StageRun.updated_at.desc(), StageRun.id.desc())
        .limit(1)
    ).first()
    default = session.scalar(
        select(ExecutionProfile)
        .where(ExecutionProfile.is_system_default)
        .order_by(ExecutionProfile.version.desc(), ExecutionProfile.id.desc())
    )
    steps = []
    selections = {item.stage: item for item in default.stage_selections} if default else {}
    for stage, label, icon in (
        ("transcribe", "Transcription", "waveform"),
        ("diarize", "Speaker recognition", "activity"),
        ("summarize", "Summaries", "note"),
    ):
        selection = selections.get(stage)
        state, checked_at = "unknown", None
        worker_key = None
        if not getattr(settings.pipeline, stage):
            state = "disabled"
        elif selection:
            connection = selection.connection
            health = (selection.model_entry.capabilities or {}).get("health") or {}
            state, checked_at = _check_state(health, now)
            if connection.key.startswith("worker:"):
                worker_key = connection.key.removeprefix("worker:")
                worker = session.scalar(select(RemoteWorker).where(RemoteWorker.key == worker_key))
                if worker is None or not worker.enabled:
                    state, checked_at = "unavailable", None
                else:
                    worker_state, worker_checked = _check_state(worker.health or {}, now)
                    if worker_state in ("degraded", "unavailable"):
                        state, checked_at = worker_state, worker_checked
        steps.append(
            {
                "label": label,
                "icon": icon,
                "state": state,
                "checked_at": checked_at,
                "worker": worker_key is not None,
            }
        )
    waiting = sum(
        counts.get(status, 0) for status in (FileStatus.discovered, FileStatus.downloaded)
    )
    active = sum(
        counts.get(status, 0) for status in (FileStatus.downloading, FileStatus.processing)
    )
    attention = session.scalar(
        select(func.count())
        .select_from(PlaudFile)
        .where(
            PlaudFile.is_trash.is_(False),
            PlaudFile.status.in_((FileStatus.error, FileStatus.partial))
            | PlaudFile.id.in_(failed_stages),
        )
    )
    if attention:
        heading, link, action = "Recordings need attention", "/?state=attention", "View recordings"
        if issue:
            link, action = f"/file/{issue.file_id}", "Open recording"
    elif any(step["state"] in ("degraded", "unavailable") for step in steps):
        heading, link, action = "Processing needs attention", "/status", "System status"
        if any(step["worker"] and step["state"] in ("degraded", "unavailable") for step in steps):
            link, action = "/settings#remote-workers", "Check remote workers"
    elif active or waiting:
        heading, link, action = "Processing activity", "/?state=generating", "View recordings"
    elif not plaud_connected:
        heading, link, action = "Plaud sign-in required", "/settings#plaud-account", "Plaud account"
    else:
        heading, link, action = "Queue empty", "/status", "System status"
    return {
        "heading": heading,
        "link": link,
        "action": action,
        "plaud_connected": plaud_connected,
        "waiting": waiting,
        "active": active,
        "attention": attention,
        "issue_stage": issue.stage.value if issue else None,
        "steps": steps,
    }
