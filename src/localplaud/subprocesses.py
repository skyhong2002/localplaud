"""Deadline-bound local processes with bounded output and recording-lease fencing.

Mac/Linux workers own a new process group per invocation. Cleanup also kills
descendants, including when a parent exits but a descendant keeps its pipes open.
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


def _terminate_group(process: subprocess.Popen) -> None:
    # Never target the host's group: start_new_session makes pid == pgid.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # The parent may have exited while a child ignored SIGTERM.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
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
    recording lease once per second. Every exceptional exit terminates/reaps the
    process group before returning capacity to its caller.
    """
    if not math.isfinite(timeout) or timeout <= 0 or output_limit < 1:
        raise ValueError("a finite positive timeout and output limit are required")
    check_cancelled()
    deadline = time.monotonic() + timeout
    tails = {"stdout": bytearray(), "stderr": bytearray()}
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
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
        _terminate_group(process)
        raise
    finally:
        # A successful parent can still leave descendants in its group.
        if process.poll() is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.stdout.close()
        process.stderr.close()
