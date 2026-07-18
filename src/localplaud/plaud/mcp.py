"""Read-only adapter for Plaud's official local MCP stdio server."""

from __future__ import annotations

import json
import logging
import select
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from ..config import PlaudMcpConfig
from .common import (
    _MAX_AUDIO_BYTES,
    PlaudAuthError,
    PlaudError,
    _assert_safe_fetch_url,
    _ext_from_url,
)
from .models import PlaudFileDTO
from .official import _cloud_notes, _parse_iso_ms, _transcript_from_source_list

log = logging.getLogger(__name__)


class PlaudMcpClient:
    """Duck-type-compatible Plaud client backed by ``@plaud-ai/mcp``.

    The MCP process is local, but its tools read the authenticated user's Plaud
    cloud data. localplaud calls only read tools. Plaud transcripts and notes
    remain migration/debug inputs under the existing artifact-mode policy.
    """

    def __init__(self, cfg: PlaudMcpConfig):
        self.cfg = cfg
        self._next_id = 0
        self._lock = threading.Lock()
        self._detail_cache: dict[str, dict] = {}
        try:
            self._process = subprocess.Popen(
                [cfg.command, *cfg.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise PlaudError(f"Could not start Plaud MCP: {exc}") from exc
        try:
            self._request(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "localplaud", "version": "1"},
                },
            )
            self._notify("notifications/initialized")
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if process.stdin is not None:
            process.stdin.close()
        if process.stdout is not None:
            process.stdout.close()
        self._process = None

    def __enter__(self) -> PlaudMcpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _write(self, message: dict[str, Any]) -> None:
        if self._process.stdin is None:
            raise PlaudError("Plaud MCP stdin is unavailable")
        self._process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self._process.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            )
            if self._process.stdout is None:
                raise PlaudError("Plaud MCP stdout is unavailable")
            while True:
                ready, _, _ = select.select(
                    [self._process.stdout], [], [], self.cfg.request_timeout_seconds
                )
                if not ready:
                    raise PlaudError(f"Plaud MCP {method} timed out")
                line = self._process.stdout.readline()
                if not line:
                    raise PlaudError("Plaud MCP disconnected")
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if response.get("id") != request_id:
                    continue
                if "error" in response:
                    error = response["error"]
                    message = (
                        error.get("message", str(error)) if isinstance(error, dict) else str(error)
                    )
                    if "auth" in message.casefold() or "login" in message.casefold():
                        raise PlaudAuthError(
                            f"{message} — run `npx -y @plaud-ai/mcp@latest install` to sign in"
                        )
                    raise PlaudError(f"Plaud MCP {method} failed: {message}")
                return response.get("result")

    def _call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            text = " ".join(item.get("text", "") for item in result.get("content", []))
            raise PlaudError(f"Plaud MCP {name} failed: {text or 'unknown error'}")
        for item in result.get("content", []):
            if item.get("type") == "text":
                try:
                    return json.loads(item.get("text", ""))
                except json.JSONDecodeError:
                    return item.get("text")
        return result.get("structuredContent") or {}

    def check_auth(self) -> dict:
        result = self._call_tool("get_current_user")
        return result if isinstance(result, dict) else {"status": "authenticated"}

    @staticmethod
    def auth_status(cfg: PlaudMcpConfig) -> dict:
        path = cfg.tokens_path.expanduser()
        return {
            "ok": path.is_file(),
            "detail": "Plaud MCP OAuth configured"
            if path.is_file()
            else "Plaud MCP sign-in required",
            "tokens_path": str(path),
        }

    def iter_files(
        self, include_trash: bool = False, page_size: int = 100
    ) -> Iterator[PlaudFileDTO]:
        del include_trash  # The official MCP does not expose trash.
        page = 1
        page_size = min(max(page_size, 10), 100)
        while True:
            payload = self._call_tool("list_files", {"page": page, "page_size": page_size})
            items = (
                payload.get("data", payload.get("files", []))
                if isinstance(payload, dict)
                else payload
            )
            items = items or []
            for item in items:
                duration = item.get("duration")
                start = _parse_iso_ms(item.get("start_at"))
                duration_ms = int(duration) if duration not in (None, "") else None
                yield PlaudFileDTO(
                    id=item["id"],
                    filename=item.get("name") or "",
                    duration=duration_ms,
                    start_time=start,
                    end_time=start + duration_ms
                    if start is not None and duration_ms is not None
                    else None,
                    serial_number=item.get("serial_number"),
                )
            if len(items) < page_size:
                break
            page += 1

    def get_detail(self, file_id: str) -> dict:
        if file_id not in self._detail_cache:
            detail = self._call_tool("get_file", {"file_id": file_id})
            if not isinstance(detail, dict):
                raise PlaudError(f"Plaud MCP returned invalid detail for {file_id}")
            self._detail_cache[file_id] = detail
        return self._detail_cache[file_id]

    def download_audio(self, file: PlaudFileDTO, dest_dir: Path) -> Path:
        url = self.get_detail(file.id).get("presigned_url")
        if not url:
            raise PlaudError(f"Plaud MCP returned no presigned_url for {file.id}")
        _assert_safe_fetch_url(url)
        dest = dest_dir / f"audio.{_ext_from_url(url, default='mp3')}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with (
            httpx.Client(timeout=120, follow_redirects=False) as raw,
            raw.stream("GET", url) as response,
        ):
            response.raise_for_status()
            with dest.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1 << 16):
                    written += len(chunk)
                    if written > _MAX_AUDIO_BYTES:
                        handle.close()
                        dest.unlink(missing_ok=True)
                        raise PlaudError(
                            f"audio for {file.id} exceeds {_MAX_AUDIO_BYTES} bytes; aborting"
                        )
                    handle.write(chunk)
        return dest

    def get_cloud_summary_md(self, file_id: str, detail: dict | None = None) -> str | None:
        notes = self.get_cloud_notes(file_id, detail)
        summary = next((note for note in notes if note["key"] == "auto_sum_note"), None)
        if summary is None:
            summary = next(
                (note for note in notes if "sum" in note["key"].casefold()),
                notes[0] if notes else None,
            )
        return summary["markdown"] if summary is not None else None

    def get_cloud_notes(self, file_id: str, detail: dict | None = None) -> list[dict]:
        detail = detail if detail is not None else self.get_detail(file_id)
        if "note_list" in detail:
            return _cloud_notes(detail)
        result = self._call_tool("get_note", {"file_id": file_id})
        # The MCP get_note tool answers with raw note_list entries.
        if isinstance(result, list):
            return _cloud_notes({"note_list": result, "source_list": []})
        if isinstance(result, str):
            markdown = result
        elif isinstance(result, dict):
            markdown = result.get("content") or result.get("markdown") or result.get("note")
        else:
            markdown = None
        if not markdown:
            return []
        return _cloud_notes(
            {
                "note_list": [
                    {
                        "data_type": "auto_sum_note",
                        "data_content": markdown,
                        "download_link_map": (
                            result.get("download_link_map", {})
                            if isinstance(result, dict)
                            else {}
                        ),
                    }
                ]
            }
        )

    def get_cloud_transcript_segments(
        self, file_id: str, detail: dict | None = None
    ) -> list[dict] | None:
        detail = detail if detail is not None else self.get_detail(file_id)
        if isinstance(detail, dict) and detail.get("source_list"):
            return _transcript_from_source_list(detail["source_list"], context=file_id)
        # The MCP get_transcript tool answers with the raw source_list entries.
        result = self._call_tool("get_transcript", {"file_id": file_id})
        if isinstance(result, list):
            return _transcript_from_source_list(result, context=file_id)
        if isinstance(result, dict):
            segments = result.get("segments", result.get("data"))
            if segments:
                return segments
            if result.get("source_list"):
                return _transcript_from_source_list(result["source_list"], context=file_id)
        return None
