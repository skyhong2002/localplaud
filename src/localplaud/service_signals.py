"""Scoped, cooperative service shutdown; importing this module installs nothing."""

from __future__ import annotations

import signal
import threading
import time


class ServiceSignals:
    """Keep TERM/INT cooperative until the owning service has finished cleanup.

    Uvicorn temporarily replaces these handlers, then restores and replays its
    captured signals. Replays and repeated signals must never interrupt draining.
    Non-main-thread callers retain the host's signal policy.
    """

    def __init__(self, on_stop=None):
        self.on_stop = on_stop
        self.requested = False
        self._previous = {}

    def request_stop(self, signum=None, frame=None):
        if self.requested:
            return
        # Set this first so repeated signals cannot reenter the stop callback.
        self.requested = True
        if self.on_stop is not None:
            self.on_stop()

    def wait(self, timeout):
        # A signal handler must not acquire the same Event/Condition lock that
        # the main thread's wait might hold when the signal interrupts it.
        deadline = time.monotonic() + timeout
        while not self.requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))
        return True

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            try:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    self._previous[sig] = signal.signal(sig, self.request_stop)
            except BaseException:
                self.__exit__(None, None, None)
                raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)
        self._previous.clear()
