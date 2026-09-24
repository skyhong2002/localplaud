"""Independent discovery and processing loops for the owned background service."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

from .claims import processing_owner

log = logging.getLogger(__name__)


class DaemonJobs:
    """A slow provider cannot prevent discovery; each loop remains single-flight."""

    def __init__(self, settings, owner: str, scheduler):
        self.settings = settings
        self.owner = owner
        self.scheduler = scheduler
        self.stopped = threading.Event()
        self.sync_lock = threading.Lock()
        self.work_lock = threading.Lock()

    def install(self):
        from ..poller.poll import _DAEMON_HEARTBEAT_INTERVAL_SECONDS

        self.scheduler.add_job(
            self.heartbeat,
            "interval",
            seconds=_DAEMON_HEARTBEAT_INTERVAL_SECONDS,
            id="daemon-heartbeat",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.work,
            "interval",
            seconds=min(30, self.settings.poller.interval_seconds),
            id="worker",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(UTC),
        )
        if self.settings.poller.enabled:
            self.scheduler.add_job(
                self.sync,
                "interval",
                seconds=self.settings.poller.interval_seconds,
                id="sync",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(UTC),
            )

    def heartbeat(self):
        from ..poller.poll import refresh_daemon_owner

        if not refresh_daemon_owner(self.owner):
            self.stopped.set()
            log.error("Daemon ownership lost; discovery and automatic processing paused")

    def _run(self, lock, operation):
        if self.stopped.is_set() or not lock.acquire(blocking=False):
            return None
        try:
            if self.stopped.is_set():
                return None
            with processing_owner(self.owner):
                return operation()
        except Exception:
            log.exception(
                "Automatic %s failed; the next scheduled attempt remains enabled",
                operation.__name__,
            )
            return None
        finally:
            lock.release()

    def sync(self):
        from ..poller.poll import poll_once

        if not self.settings.poller.enabled:
            return None

        def discover():
            result = poll_once(self.settings)
            # Wake an idle worker immediately after downloads, even if the next
            # periodic queue check is not due. A busy worker stays single-flight.
            if not self.stopped.is_set():
                self.scheduler.modify_job("worker", next_run_time=datetime.now(UTC))
            return result

        return self._run(self.sync_lock, discover)

    def work(self):
        from ..cli import process_automatic_pending

        def process():
            return process_automatic_pending(self.settings, daemon_owner=self.owner)

        return self._run(self.work_lock, process)
