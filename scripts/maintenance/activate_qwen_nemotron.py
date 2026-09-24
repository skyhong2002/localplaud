"""Opt-in rollout: switch the default speech stages and resume unfinished audio.

Run on the controller after deploying/probing the speech worker. No existing
transcripts, edits, or stage artifacts are deleted. --apply requires a drained
worker/controller so profile changes cannot race with in-flight jobs.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from localplaud.db.models import (
    FileStatus,
    PlaudFile,
    ProviderConnection,
    RecordingProfileOverride,
    RemoteWorker,
)
from localplaud.db.session import session_scope
from localplaud.providers.contracts import Capability, Health, StageCapabilities
from localplaud.providers.service import (
    create_profile_version,
    list_profiles,
    save_connection,
    save_model,
)
from localplaud.remote.registry import check_worker
from localplaud.worker.pipeline import processing_claim_active, reset_pipeline_retry

ASR = "Qwen/Qwen3-ASR-1.7B-hf"
ALIGN = "Qwen/Qwen3-ForcedAligner-0.6B-hf"
DIARIZE = "nvidia/Nemotron-3-Diarization"


def activate(worker_key: str, *, apply: bool = False):
    with session_scope() as session:
        pending = list(
            session.scalars(
                select(PlaudFile).where(
                    PlaudFile.is_trash.is_(False), PlaudFile.status != FileStatus.done
                )
            )
        )
        counts = {
            "unfinished": len(pending),
            "without_transcript": sum(r.local_transcript is None for r in pending),
            "need_download": sum(not r.audio_path for r in pending),
        }
        if not apply:
            return counts
        if any(processing_claim_active(row) for row in session.scalars(select(PlaudFile))):
            raise RuntimeError("Wait for active processing to finish before switching profiles")
        worker = session.scalar(select(RemoteWorker).where(RemoteWorker.key == worker_key))
        if worker is None:
            raise ValueError("Register the worker before activation")
        status = check_worker(session, worker.id)
        if status.get("status") != "healthy":
            raise RuntimeError("Worker health check failed")
        caps = worker.capabilities
        offered = {(c["stage"], m) for c in caps for m in c.get("models", [])}
        if not {("transcribe", ASR), ("diarize", DIARIZE)} <= offered:
            raise RuntimeError("The worker does not advertise Qwen and Nemotron")
        connection_key = "builtin:qwen-alignment-validation"
        connection = session.scalar(
            select(ProviderConnection).where(ProviderConnection.key == connection_key)
        )
        if connection is None:
            saved = save_connection(
                session,
                {
                    "key": connection_key,
                    "name": "Qwen forced-alignment validation",
                    "provider_type": "provider-word-timestamps",
                    "execution_target": "local",
                    "data_egress": False,
                    "config": {},
                },
            )
            save_model(
                session,
                {
                    "connection_id": saved["id"],
                    "model_key": ALIGN,
                    "display_name": "Qwen3 ForcedAligner 0.6B (computed with ASR)",
                    "capabilities": Capability(
                        execution_target="local",
                        data_egress=False,
                        health=Health(status="healthy"),
                        stages=(StageCapabilities(stage="align", timestamps="word"),),
                    ).model_dump(mode="json"),
                },
            )
        current = next(p for p in list_profiles(session) if p["is_system_default"])
        stages = dict(current["stages"])
        speech = {
            "transcribe": {"connection": f"worker:{worker_key}", "model": ASR, "options": {}},
            "align": {"connection": connection_key, "model": ALIGN, "options": {}},
            "diarize": {
                "connection": f"worker:{worker_key}",
                "model": DIARIZE,
                "options": {"revision": "a435e9867d79e789e90053f9b6d6834053af564a"},
            },
        }
        stages.update(speech)
        profile = create_profile_version(
            session,
            {
                "key": "qwen-nemotron",
                "name": "Qwen＋Nemotron · 靜音略過",
                "is_system_default": True,
                "privacy_policy": current["policy"]["privacy_policy"],
                "no_egress": current["policy"]["no_egress"],
                "cost_ceiling": current["policy"]["cost_ceiling"],
                "fallback_policy": current["policy"]["fallback_policy"],
                "stages": stages,
            },
        )
        for row in pending:
            override = session.get(RecordingProfileOverride, row.id)
            if override is None:
                override = RecordingProfileOverride(
                    file_id=row.id,
                    profile_id=current["id"],
                    stage_overrides={},
                    policy_overrides={},
                )
                session.add(override)
            patch = dict(override.stage_overrides or {})
            if row.local_transcript is None:
                patch.update(speech)
            else:
                # Preserve the resolved alignment model for existing ASR. The
                # new default's integrated aligner has never processed these.
                # Current old default is retained as an immutable version.
                patch["diarize"] = speech["diarize"]
            override.stage_overrides = patch
            row.process_overlong = True
            reset_pipeline_retry(row)
            # Missing speech outranks derived-stage retries in the durable queue.
            row.status = (
                FileStatus.partial if row.local_transcript is not None
                else FileStatus.downloaded if row.audio_path else FileStatus.discovered
            )
        return counts | {"profile_id": profile["id"], "queued": len(pending)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-key", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(activate(args.worker_key, apply=args.apply), ensure_ascii=False))
