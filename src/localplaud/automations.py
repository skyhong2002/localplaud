"""Local AutoFlow matching, idempotent execution, and audit history."""

from __future__ import annotations

from sqlalchemy import select

from .db.models import (
    AutomationRule,
    AutomationRun,
    ExecutionProfile,
    Folder,
    NoteTemplate,
    PlaudFile,
    RecordingProfileOverride,
    StageName,
    StageRun,
    StageStatus,
    Tag,
)
from .db.session import session_scope


def rule_sentence(rule: AutomationRule | dict) -> str:
    trigger = rule.trigger if isinstance(rule, AutomationRule) else rule.get("trigger", {})
    actions = rule.actions if isinstance(rule, AutomationRule) else rule.get("actions", {})
    conditions = []
    if trigger.get("origin"):
        conditions.append(f"source is {trigger['origin']}")
    if trigger.get("title_contains"):
        conditions.append(f'title contains “{trigger["title_contains"]}”')
    if trigger.get("min_duration_minutes") is not None:
        conditions.append(f"duration is at least {trigger['min_duration_minutes']} min")
    if trigger.get("max_duration_minutes") is not None:
        conditions.append(f"duration is at most {trigger['max_duration_minutes']} min")
    if trigger.get("folder_id") is not None:
        conditions.append(f"folder is #{trigger['folder_id']}")
    if trigger.get("tag_id") is not None:
        conditions.append(f"tag includes #{trigger['tag_id']}")
    effects = []
    if actions.get("note_template_key"):
        effects.append(f"use {actions['note_template_key']} notes")
    if actions.get("profile_id") is not None:
        effects.append(f"use execution profile #{actions['profile_id']}")
    if actions.get("folder_id") is not None:
        effects.append(f"move to folder #{actions['folder_id']}")
    if actions.get("add_tag_ids"):
        effects.append("add tags " + ", ".join(f"#{value}" for value in actions["add_tag_ids"]))
    return f"When {' and '.join(conditions) or 'a recording arrives'}, then {', '.join(effects) or 'record the match'}."


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
    return True, reasons or ["all recordings"]


def _mark_notes_stale(session, file_id: str) -> None:
    for stage in (StageName.summarize, StageName.mind_map):
        run = session.scalar(
            select(StageRun).where(StageRun.file_id == file_id, StageRun.stage == stage)
        )
        if run is not None:
            run.status = StageStatus.pending
            run.detail = (run.detail or {}) | {"stale": True, "reason": "AutoFlow changed notes"}
            run.error = None


def _apply_actions(session, rule: AutomationRule, recording: PlaudFile) -> dict:
    actions = rule.actions or {}
    applied: dict = {}
    if key := actions.get("note_template_key"):
        recording.note_template_key = key
        _mark_notes_stale(session, recording.id)
        applied["note_template_key"] = key
    if actions.get("profile_id") is not None:
        profile_id = int(actions["profile_id"])
        override = session.get(RecordingProfileOverride, recording.id)
        if override is None:
            session.add(RecordingProfileOverride(file_id=recording.id, profile_id=profile_id))
        else:
            override.profile_id = profile_id
        applied["profile_id"] = profile_id
    if actions.get("folder_id") is not None:
        recording.folder_id = int(actions["folder_id"])
        applied["folder_id"] = recording.folder_id
    if actions.get("add_tag_ids"):
        existing = {tag.id for tag in recording.tags}
        tags = list(
            session.scalars(select(Tag).where(Tag.id.in_(actions["add_tag_ids"])))
        )
        recording.tags.extend(tag for tag in tags if tag.id not in existing)
        applied["add_tag_ids"] = [tag.id for tag in tags]
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
                with session.begin_nested():
                    applied = _apply_actions(session, rule, recording)
                run = AutomationRun(
                    rule_id=rule.id,
                    rule_version=rule.version,
                    file_id=file_id,
                    status="completed",
                    matched=True,
                    detail={
                        "reasons": reasons,
                        "applied": applied,
                        "notification_requested": rule.notify,
                    },
                )
                session.add(run)
                results.append({"rule_id": rule.id, "status": "completed", "applied": applied})
            except Exception as exc:  # noqa: BLE001
                session.add(
                    AutomationRun(
                        rule_id=rule.id,
                        rule_version=rule.version,
                        file_id=file_id,
                        status="failed",
                        matched=True,
                        detail={"reasons": reasons},
                        error=str(exc)[:2000],
                    )
                )
                results.append({"rule_id": rule.id, "status": "failed", "error": str(exc)})
    return results


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
