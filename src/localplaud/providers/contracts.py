"""Provider-neutral capability declarations used by profile resolution.

Capabilities are deliberately stage-specific.  In particular, an
OpenAI-compatible text endpoint does not imply audio or embeddings support.
"""

from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any


class CapabilityKind(enum.StrEnum):
    asr = "asr"
    alignment = "alignment"
    diarization = "diarization"
    correction = "correction"
    notes = "notes"
    mind_map = "mind_map"
    embeddings = "embeddings"
    ask = "ask"


class HealthState(enum.StrEnum):
    unknown = "unknown"
    healthy = "healthy"
    degraded = "degraded"
    unavailable = "unavailable"


class EgressPolicy(enum.StrEnum):
    local_only = "local_only"
    trusted_worker = "trusted_worker"
    external = "external"


@dataclass(frozen=True)
class CapabilitySpec:
    kind: CapabilityKind
    languages: tuple[str, ...] = ("*",)
    timestamps: bool = False
    word_timestamps: bool = False
    speaker_output: bool = False
    streaming: bool = False
    batch: bool = True
    prompt_limit: int | None = None
    input_limit: int | None = None
    required_hardware: tuple[str, ...] = ()
    egress: EgressPolicy = EgressPolicy.local_only
    health: HealthState = HealthState.unknown
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def supports_language(self, language: str) -> bool:
        return "*" in self.languages or language in self.languages
