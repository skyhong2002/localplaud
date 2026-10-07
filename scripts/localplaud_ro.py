"""Hermes Local Plaud MCP tools using the existing account-password login.

Tool names remain compatible with the deployed localplaud-readonly adapter.
Content operations use only GET; POST is reserved for creating a login session.
The completion notification job and its token are independent of this adapter.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import urllib.error
from pathlib import Path
from urllib.parse import urlencode

from localplaud_read import Reader, ReadError
from mcp.server.fastmcp import FastMCP

ACCOUNT_PATH = Path(os.environ.get(
    "LOCALPLAUD_ACCOUNT_FILE", str(Path.home() / ".hermes/localplaud_account.json")
))
_LOCK = threading.Lock()
mcp = FastMCP("localplaud-readonly")


def _request(path: str, *, params: dict[str, str] | None = None) -> str:
    if params:
        path += "?" + urlencode(params)
    try:
        with _LOCK, ACCOUNT_PATH.with_suffix(".lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            return Reader(ACCOUNT_PATH).read(path)
    except ReadError:
        raise
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        raise ReadError("Check localplaud connection and private account/session files") from None


def _json(path: str, *, params: dict[str, str] | None = None) -> object:
    try:
        return json.loads(_request(path, params=params))
    except json.JSONDecodeError:
        raise ReadError("Localplaud returned invalid JSON") from None


def _file_id(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Provide a valid recording ID")
    return value


def _content(file_id: str, suffix: str, max_chars: int, offset: int) -> str:
    text = _request(f"/file/{_file_id(file_id)}/{suffix}")
    max_chars = max(1000, min(max_chars, 30000))
    if offset < 0 or offset > len(text):
        raise ValueError("offset must be within the returned document's total_chars")
    end = min(offset + max_chars, len(text))
    continuation = f"next_offset={end}; call this same tool again to continue" if end < len(text) else "end_of_document=true"
    return f"[offset={offset}; total_chars={len(text)}; {continuation}]\n\n{text[offset:end]}"


@mcp.tool()
def localplaud_diagnostics() -> str:
    """Return Local Plaud's redacted runtime and aggregate diagnostics."""
    return json.dumps(_json("/api/system/diagnostics.json"), ensure_ascii=False)


@mcp.tool()
def localplaud_list_recordings(query: str = "", state: str = "", max_results: int = 20) -> str:
    """List bounded Local Plaud recording metadata; never downloads audio."""
    max_results = max(1, min(max_results, 50))
    params = {}
    if query.strip():
        params["q"] = query.strip()
    if state.strip():
        params["state"] = state.strip()
    payload = _json("/api/files", params=params)
    files = payload.get("files", []) if isinstance(payload, dict) else []
    return json.dumps({"count": len(files), "returned": min(len(files), max_results),
                       "files": files[:max_results]}, ensure_ascii=False)


@mcp.tool()
def localplaud_recording_page(file_id: str, max_chars: int = 16000, offset: int = 0) -> str:
    """Read canonical transcript and notes as Markdown, without audio or HTML UI.

    If next_offset is returned, call again with that offset until end_of_document.
    Do not treat the first portion of a long recording as its complete content.
    """
    return _content(file_id, "export.md", max_chars, offset)


@mcp.tool()
def localplaud_recording_usage(file_id: str) -> str:
    """Read one recording's processing usage and cost ledger."""
    return json.dumps(_json(f"/api/files/{_file_id(file_id)}/usage"), ensure_ascii=False)


@mcp.tool()
def localplaud_recording_transcript(file_id: str, max_chars: int = 16000, offset: int = 0) -> str:
    """Read corrected transcript with timestamps and speakers; follow next_offset to the end."""
    return _content(file_id, "export/transcript.txt", max_chars, offset)


@mcp.tool()
def localplaud_recording_notes(file_id: str, max_chars: int = 16000, offset: int = 0) -> str:
    """Read generated and edited notes as Markdown; follow next_offset to the end."""
    return _content(file_id, "export/notes.md", max_chars, offset)


if __name__ == "__main__":
    mcp.run()
