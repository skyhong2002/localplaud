"""Ordered, explicit profile fallback selection and retry classification."""

from __future__ import annotations

import copy

from ..asr.base import AsrUnavailable
from ..embeddings.base import EmbeddingUnavailable
from ..llm.base import LLMUnavailable
from ..remote.client import RemoteWorkerError
from ..worker.align import AlignmentUnavailable
from ..worker.diarize import DiarizationUnavailable
from .usage import CostPolicyError


def candidate_snapshots(snapshot: dict, stage: str) -> list[dict]:
    """Return primary then ordered fallback snapshots for one stage."""
    primary = copy.deepcopy(snapshot)
    primary["fallback"] = {"stage": stage, "index": 0, "primary": True}
    result = [primary]
    candidates = (
        ((snapshot.get("policy") or {}).get("fallback_policy") or {})
        .get("stages", {})
        .get(stage, [])
    )
    for index, selection in enumerate(candidates, start=1):
        candidate = copy.deepcopy(snapshot)
        candidate["stages"][stage] = copy.deepcopy(selection)
        candidate["fallback"] = {"stage": stage, "index": index, "primary": False}
        result.append(candidate)
    return result


def is_retryable_fallback_error(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            AsrUnavailable,
            AlignmentUnavailable,
            DiarizationUnavailable,
            LLMUnavailable,
            EmbeddingUnavailable,
            CostPolicyError,
        ),
    ):
        return True
    return isinstance(exc, RemoteWorkerError) and exc.retryable
