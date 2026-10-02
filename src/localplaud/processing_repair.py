"""Serial, resumable operator-requested recovery through the normal Web API.

This never changes providers, quota floors, artifact contents, or completion flags.
The private manifest limits recovery to the explicitly selected recordings.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select

from .config import get_settings
from .db.models import FileStatus, PlaudFile
from .db.session import session_scope
from .llm.codex_local import CodexLocalLLM
from .providers.service import resolve_recording_profile
from .worker.pipeline import _settings_for_stage, new_recordings_waiting, processing_claim_active

SPEECH_STAGES = {"convert", "transcribe", "align", "diarize"}


def needs_model_upgrade(item: dict, state: dict) -> bool:
    """An explicit migration must not silently reuse the previous ASR model."""
    return bool(
        item.get("target_asr_model")
        and state.get("transcribe_model")
        and state["transcribe_model"] != item["target_asr_model"]
    )


def recovery_action(item: dict, state: dict, text_available: bool) -> str:
    if state.get("missing") or state.get("trash"):
        return "skipped"
    if state["active"]:
        return "running"
    if state["status"] == "done" and not needs_model_upgrade(item, state):
        return "completed"
    if item.get("attempts", 0) >= 3:
        return "needs_attention"
    # One acoustic recovery is useful even while the text provider is paused.
    # Do not repeatedly run the same speech failure while waiting for quota.
    speech_failed = needs_model_upgrade(item, state) or any(
        name in SPEECH_STAGES and status in {"failed", "degraded", "pending"}
        for name, status in state["stages"].items()
    )
    if not text_available and not (speech_failed and not item.get("speech_attempted")):
        return "waiting_for_provider"
    return "resume"


def save_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def recovery_endpoint(item: dict, state: dict) -> str:
    base = f"/file/{item['id']}"
    if needs_model_upgrade(item, state):
        return base + "/reprocess?force=true"
    stages = state["stages"]
    if (
        state.get("has_local_transcript")
        and stages.get("transcribe") == "completed"
        and all(stages.get(stage, "skipped") in {"completed", "skipped"} for stage in SPEECH_STAGES)
    ):
        return base + "/generate-notes"
    return base + "/reprocess"


def recording_state(file_id: str) -> dict:
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        if row is None:
            return {"missing": True}
        return {
            "status": str(row.status),
            "active": processing_claim_active(row),
            "trash": row.is_trash,
            "has_local_transcript": row.local_transcript is not None,
            "stages": {str(run.stage): str(run.status) for run in row.stage_runs},
            "transcribe_model": next(
                (
                    t.model
                    for t in sorted(row.transcripts, key=lambda t: t.id, reverse=True)
                    if t.source == "local"
                ),
                None,
            ),
        }


def text_provider_health(file_id: str, cache: dict) -> tuple[bool, str]:
    settings = get_settings()
    with session_scope() as session:
        row = session.get(PlaudFile, file_id)
        snapshot = resolve_recording_profile(
            session,
            file_id,
            template_key=row.note_template_key or settings.pipeline.summary_template,
        ).to_dict()
    for stage in ("correct", "summarize", "mind_map"):
        selected = _settings_for_stage(settings, snapshot, stage).llm
        if selected.provider != "codex-local":
            continue
        key = selected.codex_local.model_dump_json()
        cached = cache.get(key)
        if cached is None or time.monotonic() - cached[0] > 60:
            cache[key] = (time.monotonic(), CodexLocalLLM(selected.codex_local).health())
        healthy, detail = cache[key][1]
        if not healthy:
            return False, detail
    return True, "Configured text providers may run"


def profile_matches(file_id: str, required: dict) -> bool:
    with session_scope() as session:
        selected = resolve_recording_profile(session, file_id).to_dict()["stages"]
    return all(
        all(selected.get(stage, {}).get(key) == value for key, value in choice.items())
        for stage, choice in required.items()
    )


def priority_work_pending(settings) -> bool:
    """Maintenance yields to new downloads and work already using the pipeline."""
    with session_scope() as session:
        now = datetime.now(UTC)
        if new_recordings_waiting(session, settings, now):
            return True
        return any(
            processing_claim_active(row, now=now)
            for row in session.scalars(
                select(PlaudFile).where(PlaudFile.status == FileStatus.processing)
            )
        )


def run_repair_queue(path: Path, *, base_url: str = "http://127.0.0.1:8080") -> None:
    import fcntl

    lock_path = path.with_suffix(".lock")
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        manifest = json.loads(path.read_text())
        cache: dict = {}
        settings = get_settings()
        with httpx.Client(
            base_url=base_url,
            headers={"X-Auth-Token": settings.api.auth_token or ""},
            timeout=30,
        ) as client:
            while True:
                running = False
                pending = False
                for item in manifest["recordings"]:
                    if item.get("outcome") in {"completed", "skipped", "needs_attention"}:
                        continue
                    state = recording_state(item["id"])
                    item["observed"] = state
                    if (
                        state.get("missing")
                        or state.get("trash")
                        or state.get("active")
                        or state.get("status") == "done"
                    ):
                        healthy, reason = True, ""
                    else:
                        try:
                            healthy, reason = text_provider_health(item["id"], cache)
                        except Exception as exc:
                            healthy, reason = False, type(exc).__name__
                    action = recovery_action(item, state, healthy)
                    item["outcome"] = action
                    item["provider_status"] = reason
                    if action == "running":
                        # Adopt an already requested recovery without starting another.
                        item["speech_attempted"] = True
                        running = True
                        break
                    if action == "waiting_for_provider":
                        pending = True
                        continue
                    if action != "resume":
                        continue
                    if priority_work_pending(settings):
                        item["outcome"] = "waiting_for_priority_work"
                        pending = True
                        break
                    required = manifest.get("required_stages")
                    if required and not profile_matches(item["id"], required):
                        item["outcome"] = "needs_attention"
                        item["provider_status"] = "Selected profile changed; recovery not submitted"
                        continue
                    # Persist intent before POST: a connection loss must not cause a
                    # second recovery before the durable processing claim is checked.
                    item["attempts"] = item.get("attempts", 0) + 1
                    item["speech_attempted"] = True
                    item["outcome"] = "submitting"
                    save_manifest(path, manifest)
                    try:
                        endpoint = recovery_endpoint(item, state)
                        response = client.post(endpoint)
                        if response.status_code == 409:
                            item["attempts"] -= 1
                        elif response.status_code >= 400:
                            item["last_http_status"] = response.status_code
                        item["outcome"] = (
                            "running"
                            if response.status_code in {200, 202, 409}
                            else "retry_pending"
                        )
                    except httpx.HTTPError as exc:
                        item["outcome"] = "submission_uncertain"
                        item["transport_error"] = type(exc).__name__
                    running = True
                    break
                manifest["updated_at"] = time.time()
                manifest["counts"] = {
                    name: sum(item.get("outcome") == name for item in manifest["recordings"])
                    for name in {item.get("outcome", "pending") for item in manifest["recordings"]}
                }
                save_manifest(path, manifest)
                if not running and not pending:
                    return
                time.sleep(10 if running else 60)
