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
    """Build the active workspace's Plaud client (imports lazily to keep CLI
    startup fast). Each workspace authenticates as its own Plaud account."""
    from ..config import get_settings
    from ..db.tenancy import DEFAULT_WORKSPACE_ID, current_workspace_id
    from .connections import WorkspacePlaudClient, workspace_plaud_config

    workspace_id = current_workspace_id() or DEFAULT_WORKSPACE_ID
    if workspace_id != DEFAULT_WORKSPACE_ID:
        from .official import PlaudOfficialClient

        scoped = workspace_plaud_config(cfg, get_settings())
        return WorkspacePlaudClient(PlaudOfficialClient(scoped.official), workspace_id)
    if cfg.provider == "official":
        from .official import PlaudOfficialClient

        return PlaudOfficialClient(cfg.official)
    if cfg.provider == "mcp":
        from .mcp import PlaudMcpClient

        return PlaudMcpClient(cfg.mcp)
    raise ValueError(f"unsupported Plaud provider: {cfg.provider}")
