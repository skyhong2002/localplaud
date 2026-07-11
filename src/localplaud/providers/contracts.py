"""Typed, provider-neutral capability declarations."""

from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProviderStage(enum.StrEnum):
    transcribe = "transcribe"
    align = "align"
    diarize = "diarize"
    correct = "correct"
    summarize = "summarize"
    mind_map = "mind_map"
    embed = "embed"
    ask = "ask"


class Health(BaseModel):
    model_config = ConfigDict(frozen=True)
    status: Literal["healthy", "degraded", "unavailable", "unknown"] = "unknown"
    checked_at: str | None = None
    detail: str | None = None


class StageCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)
    stage: ProviderStage
    languages: tuple[str, ...] = ("*",)
    timestamps: Literal["none", "segment", "word"] = "none"
    speaker_output: bool = False
    batch: bool = True
    streaming: bool = False
    prompt_limit: int | None = Field(default=None, ge=1)
    input_limit: int | None = Field(default=None, ge=1)
    hardware_requirement: str | None = None


class Capability(BaseModel):
    """A model's complete execution and stage contract."""

    model_config = ConfigDict(frozen=True)
    execution_target: Literal["local", "cloud", "remote_worker"]
    data_egress: bool
    health: Health = Field(default_factory=Health)
    stages: tuple[StageCapabilities, ...]
    metadata: dict[str, Any] = Field(default_factory=dict)

    def for_stage(self, stage: ProviderStage | str) -> StageCapabilities | None:
        wanted = ProviderStage(stage)
        return next((item for item in self.stages if item.stage == wanted), None)
