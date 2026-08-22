"""Logging setup — Rich console handler, one call to configure."""

from __future__ import annotations

import logging
import os

from rich.logging import RichHandler

_configured = False


def setup_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    lvl = (level or os.environ.get("LOCALPLAUD_LOG_LEVEL", "INFO")).upper()
    logging.basicConfig(
        level=lvl,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )
    # Quiet noisy libraries. APScheduler otherwise logs four INFO lines per
    # 30-second heartbeat; its skip/overrun warnings still come through.
    for noisy in ("httpx", "httpcore", "urllib3", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
