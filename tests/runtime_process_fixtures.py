"""Disposable Python stand-ins for speech -> real VAD slice -> ffmpeg."""

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager


def alive(pid):
    state = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2
    )
    return state.returncode == 0 and not state.stdout.strip().startswith("Z")


def assert_stopped(paths):
    assert all(path.exists() for path in paths), "all synthetic descendants must start"
    records = [json.loads(path.read_text()) for path in paths]
    assert records[0]["group"] != os.getpgrp(), "fixture must never own the test host group"
    assert all(record["group"] == records[0]["pid"] for record in records)
    until = time.monotonic() + 2
    while any(alive(record["pid"]) for record in records) and time.monotonic() < until:
        time.sleep(0.02)
    assert not any(alive(record["pid"]) for record in records), records


@contextmanager
def nested_vad_process(tmp_path, *, inner_timeout=120, ignore_term=False, catch_timeout=False):
    """Replace only ffmpeg's argv; never import models or decode real audio.

    Both ffmpeg and its child ignore SIGTERM and hold the capture pipes open.
    The fixture cleans up individual recorded PIDs even if group ownership fails.
    """
    paths = [tmp_path / name for name in ("runtime.pid", "slice.pid", "descendant.pid")]

    def record(path):
        return (
            f"Path({str(path)!r}).write_text(json.dumps("
            "{'pid': os.getpid(), 'group': os.getpgrp()}))\n"
        )

    descendant = (
        "import json,os,signal,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        + record(paths[2])
        + "time.sleep(60)\n"
    )
    ffmpeg = (
        "import json,os,signal,subprocess,sys,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        + record(paths[1])
        + f"subprocess.Popen([sys.executable, '-c', {descendant!r}])\n"
        + "os.write(1, b'x' * 200000)\nos.write(2, b'y' * 200000)\ntime.sleep(60)\n"
    )
    controller = (
        "import json,os,signal,sys\nfrom pathlib import Path\n"
        "from localplaud.asr import speech_runtime, vad\n"
        "from localplaud.subprocesses import run_process\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + record(paths[0])
        + "vad.shutil.which = lambda _: 'synthetic-ffmpeg'\n"
        + f"vad.run_process = lambda args, **kw: run_process("
        f"[sys.executable, '-c', {ffmpeg!r}], timeout={inner_timeout!r})\n"
        + "try:\n"
        + f"    speech_runtime.slice_region('synthetic.wav', 0, 1, {str(tmp_path / 'slice.wav')!r})\n"
        + "except vad.VadError:\n"
        + ("    print('inner timeout handled', flush=True)\n" if catch_timeout else "    raise\n")
        + "print(run_process([sys.executable, '-c', \"print('runtime survived')\"], timeout=3).stdout, end='')\n"
    )
    try:
        yield [sys.executable, "-c", controller], paths
    finally:
        # Never infer a group to kill from an escaped child PID.
        for path in reversed(paths):
            if path.exists():
                pid = json.loads(path.read_text())["pid"]
                if alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
