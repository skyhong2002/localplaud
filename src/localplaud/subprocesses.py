"""Deadline-bound local processes with bounded output and recording-lease fencing.

Mac/Linux workers own a new process group per outer invocation. Nested runners
inherit that group so cancellation reaches them even if their controller dies.
Cleanup also handles descendants that keep an exited parent's pipes open.
No command arguments or captured model output are included in timeout errors.
"""

from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Sequence

OUTPUT_TAIL_BYTES = 64 * 1024
_GROUP_ENV = "_LOCALPLAUD_SUBPROCESS_GROUP"


def _enclosing_group() -> int | None:
    """Accept only a runner's new session or its inherited concrete group.

    Popen cannot put its not-yet-known PID in the child's environment. The first
    child instead verifies its launching parent and session leadership, then
    publishes its group for further descendants (including ordinary Popen).
    Stale markers after setsid/setpgid do not confer group ownership.
    """
    marker = os.environ.get(_GROUP_ENV)
    group = os.getpgrp()
    if os.getsid(0) != group:
        return None
    if marker == str(group) or (
        marker == f"parent:{os.getppid()}" and group == os.getpid()
    ):
        return group
    return None


# Normalize before a managed runtime can fork ordinary Python children.
if (_inherited_group := _enclosing_group()) is not None:
    os.environ[_GROUP_ENV] = str(_inherited_group)


class ProcessDeadlineExceeded(TimeoutError):
    def __init__(self, timeout: float, stdout: bytes, stderr: bytes):
        super().__init__(f"local process exceeded its {timeout:g}s deadline")
        self.stdout = stdout.decode("utf-8", errors="replace")
        self.stderr = stderr.decode("utf-8", errors="replace")


def _check_processing_claim() -> None:
    from .worker.claims import current_processing_claim

    claim = current_processing_claim()
    if claim is not None:
        from .worker.pipeline import _assert_processing_claim

        _assert_processing_claim(claim.file_id, claim.token)


def _signal_process(process: subprocess.Popen, sig: int, *, owns_group: bool) -> None:
    try:
        if owns_group:
            # Only a start_new_session child belongs to this controller. Never
            # signal our own group, even if an ownership assumption is broken.
            if process.pid == os.getpgrp():
                raise RuntimeError("refusing to signal the controller's process group")
            os.killpg(process.pid, sig)
        else:
            # Nested cleanup must not kill its enclosing runtime or siblings.
            process.send_signal(sig)
    except ProcessLookupError:
        pass


def _terminate_process(process: subprocess.Popen, *, owns_group: bool) -> None:
    _signal_process(process, signal.SIGTERM, owns_group=owns_group)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # Only the outer owner kills the group. It also removes descendants of
        # an inner process whose controller exited or caught an inner timeout.
        _signal_process(process, signal.SIGKILL, owns_group=owns_group)
        process.wait(timeout=1)


def run_process(
    args: Sequence[str],
    *,
    timeout: float,
    check_cancelled: Callable[[], None] = _check_processing_claim,
    output_limit: int = OUTPUT_TAIL_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Run locally, retaining only each stream's tail in memory.

    ``check_cancelled`` raises to cancel; the default checks the caller's durable
    recording lease once per second. The outer invocation owns/reaps a group;
    nested invocations terminate/reap only their direct child on exceptions.
    All nested descendants remain subject to the outer deadline and cleanup.
    Commands must not daemonize or create their own sessions/groups.
    """
    if not math.isfinite(timeout) or timeout <= 0 or output_limit < 1:
        raise ValueError("a finite positive timeout and output limit are required")
    check_cancelled()
    deadline = time.monotonic() + timeout
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    group = _enclosing_group()
    owns_group = group is None
    environment = os.environ.copy()
    environment[_GROUP_ENV] = f"parent:{os.getpid()}" if owns_group else str(group)
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=owns_group,
        env=environment,
    )
    terminated = False
    try:
        with selectors.DefaultSelector() as selector:
            for name in tails:
                stream = getattr(process, name)
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            next_check = time.monotonic() + 1
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    raise ProcessDeadlineExceeded(timeout, tails["stdout"], tails["stderr"])
                if now >= next_check:
                    check_cancelled()
                    next_check = time.monotonic() + 1
                for key, _ in selector.select(timeout=min(0.1, deadline - now)):
                    chunk = os.read(key.fd, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    tail = tails[key.data]
                    tail.extend(chunk)
                    del tail[:-output_limit]
            check_cancelled()
            return subprocess.CompletedProcess(
                args,
                process.wait(),
                tails["stdout"].decode("utf-8", errors="replace"),
                tails["stderr"].decode("utf-8", errors="replace"),
            )
    except BaseException:
        _terminate_process(process, owns_group=owns_group)
        terminated = True
        raise
    finally:
        # A successful parent can still leave descendants in its group.
        # Do not signal a reaped group twice: on macOS a zombies-only group can
        # report EPERM and mask the original timeout/lease-cancellation error.
        if owns_group and not terminated and process.poll() is not None:
            _signal_process(process, signal.SIGKILL, owns_group=True)
        process.stdout.close()
        process.stderr.close()
