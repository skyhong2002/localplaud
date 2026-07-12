"""Plaud cloud clients (read-only).

Two official providers, selected by ``plaud.provider``:

- :class:`~localplaud.plaud.official.PlaudOfficialClient` — the official Open
  API with auto-refreshing OAuth (default).
- :class:`~localplaud.plaud.mcp.PlaudMcpClient` — the official Plaud MCP stdio
  server, with its own OAuth cache.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import PlaudConfig

if TYPE_CHECKING:
    from .mcp import PlaudMcpClient
    from .official import PlaudOfficialClient


def make_plaud_client(cfg: PlaudConfig) -> PlaudOfficialClient | PlaudMcpClient:
    """Build the configured Plaud client (imports lazily to keep CLI startup
    fast)."""
    if cfg.provider == "official":
        from .official import PlaudOfficialClient

        return PlaudOfficialClient(cfg.official)
    if cfg.provider == "mcp":
        from .mcp import PlaudMcpClient

        return PlaudMcpClient(cfg.mcp)
    raise ValueError(f"unsupported Plaud provider: {cfg.provider}")
