"""Typed views over the Plaud cloud API payloads.

We keep the raw dict too (stored on ``PlaudFile.raw``); these models just give
typed access to the fields we actually use for syncing and display.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PlaudFileDTO(BaseModel):
    """Normalized minimal metadata from an official Plaud transport."""

    model_config = ConfigDict(extra="allow")

    id: str
    filename: str = ""
    duration: int | None = None  # ms
    start_time: int | None = None  # epoch ms
    end_time: int | None = None  # epoch ms
