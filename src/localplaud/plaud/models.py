"""Typed views over the Plaud cloud API payloads.

We keep the raw dict too (stored on ``PlaudFile.raw``); these models just give
typed access to the fields we actually use for syncing and display.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PlaudFileDTO(BaseModel):
    """One entry from ``GET /file/simple/web`` -> ``data_file_list``."""

    model_config = ConfigDict(extra="allow")

    id: str
    filename: str = ""
    fullname: str | None = None
    filesize: int | None = None
    file_md5: str | None = None
    duration: int | None = None  # ms
    start_time: int | None = None  # epoch ms
    end_time: int | None = None  # epoch ms
    scene: int | None = None
    is_trash: bool = False
    version: int | None = None
    version_ms: int | None = None
    edit_time: int | None = None
    is_trans: bool = False
    is_summary: bool = False

    @property
    def audio_ext(self) -> str:
        if self.fullname and "." in self.fullname:
            return self.fullname.rsplit(".", 1)[-1].lower()
        return "opus"


class FileListResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: int = 0
    msg: str = ""
    data_file_total: int = 0
    data_file_list: list[PlaudFileDTO] = Field(default_factory=list)
