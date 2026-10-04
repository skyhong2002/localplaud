"""Ordered, bounded concurrency for independent model calls."""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor


def run_ordered(items, work, parallelism):
    """Run ``work(index, item)`` for every item; results keep item order.

    Items are independent model calls, so overlapping them only shortens wall
    time. The first failure in item order is raised after running calls finish,
    and durable per-call state keeps whatever already completed.
    """
    items = list(items)
    if parallelism <= 1 or len(items) <= 1:
        return [work(index, item) for index, item in enumerate(items)]
    with ThreadPoolExecutor(
        max_workers=min(parallelism, len(items)), thread_name_prefix="localplaud-calls"
    ) as pool:
        # Each task runs in a copy of the caller's context so usage capture and
        # the processing claim keep applying inside the worker threads.
        futures = [
            pool.submit(contextvars.copy_context().run, work, index, item)
            for index, item in enumerate(items)
        ]
        try:
            return [future.result() for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()
            raise
