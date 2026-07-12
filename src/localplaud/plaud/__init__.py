"""Plaud cloud clients (read-only).

Three interchangeable providers, selected by ``plaud.provider``:

- :class:`~localplaud.plaud.official.PlaudOfficialClient` — the official Open
  API with auto-refreshing OAuth (default).
- :class:`~localplaud.plaud.mcp.PlaudMcpClient` — the official Plaud MCP stdio
  server, with its own OAuth cache.
- :class:`~localplaud.plaud.client.PlaudClient` — the reverse-engineered
  api-apse1 web API, driven by a pasted browser session.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

from ..config import PlaudConfig

if TYPE_CHECKING:
    from .client import PlaudClient
    from .mcp import PlaudMcpClient
    from .official import PlaudOfficialClient


def make_plaud_client(cfg: PlaudConfig) -> PlaudClient | PlaudOfficialClient | PlaudMcpClient:
    """Build the configured Plaud client (imports lazily to keep CLI startup
    fast)."""
    if cfg.provider == "official":
        from .official import PlaudOfficialClient

        return PlaudOfficialClient(cfg.official)
    if cfg.provider == "mcp":
        from .mcp import PlaudMcpClient

        return PlaudMcpClient(cfg.mcp)
    warnings.warn(
        "plaud.provider='apse1' is deprecated; use the official Open API or Plaud MCP",
        DeprecationWarning,
        stacklevel=2,
    )
    from .client import PlaudClient

    return PlaudClient(cfg)
