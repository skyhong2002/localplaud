"""Synthetic processes only: deadlines, pipe pressure, cancellation and cleanup."""

import subprocess
import sys
import time
from pathlib import Path

import pytest

from localplaud.subprocesses import ProcessDeadlineExceeded, run_process
from tests.runtime_process_fixtures import assert_stopped, nested_vad_process


def test_output_is_drained_without_unbounded_capture():
    result = run_process(
        [sys.executable, "-c", "import os; os.write(1, b'x'*2000000); os.write(2, b'y'*2000000)"],
        timeout=5,
        output_limit=4096,
    )
    assert result.returncode == 0
    assert result.stdout == "x" * 4096
    assert result.stderr == "y" * 4096


def _alive(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2
    )
    return result.returncode == 0 and not result.stdout.strip().startswith("Z")


@pytest.mark.parametrize("parent_exits", [False, True])
def test_deadline_kills_group_even_if_parent_exits_or_ignores_term(tmp_path, parent_exits):
    pid_file = tmp_path / "child.pid"
    child = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent = (
        "import signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        + ("sys.exit(0)" if parent_exits else "time.sleep(60)")
    )
    started = time.monotonic()
    with pytest.raises(ProcessDeadlineExceeded) as failure:
        run_process([sys.executable, "-c", parent], timeout=0.5)
    assert time.monotonic() - started < 4
    assert str(pid_file) not in str(failure.value)
    assert pid_file.exists()
    pid = int(pid_file.read_text())
    until = time.monotonic() + 2
    while _alive(pid) and time.monotonic() < until:
        time.sleep(0.02)
    assert not _alive(pid)
    assert run_process([sys.executable, "-c", "print('recovered')"], timeout=2).stdout == "recovered\n"


@pytest.mark.parametrize("base", [RuntimeError, KeyboardInterrupt])
def test_cancellation_terminates_child_before_returning(tmp_path, base):
    pid_file = tmp_path / "cancel.pid"

    class Cancelled(base):
        pass

    def check():
        if pid_file.exists():
            raise Cancelled("synthetic cancellation")

    command = (
        "import os,time; from pathlib import Path; "
        f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    with pytest.raises(Cancelled):
        run_process([sys.executable, "-c", command], timeout=5, check_cancelled=check)
    assert not _alive(int(pid_file.read_text()))


def test_deadline_retains_bounded_diagnostics_without_rendering_them():
    with pytest.raises(ProcessDeadlineExceeded) as failure:
        run_process(
            [sys.executable, "-c", "import os,time; os.write(2,b'private fixture'*10000); time.sleep(60)"],
            timeout=0.2,
            output_limit=100,
        )
    assert len(failure.value.stderr) == 100
    assert "private fixture" not in str(failure.value)


@pytest.mark.parametrize("ignore_term", [False, True])
def test_outer_deadline_kills_nested_vad_and_pipe_holding_descendants(tmp_path, ignore_term):
    with nested_vad_process(tmp_path, ignore_term=ignore_term) as (command, paths):
        started = time.monotonic()
        with pytest.raises(ProcessDeadlineExceeded):
            run_process(command, timeout=2, output_limit=1024)
        assert time.monotonic() - started < 5
        assert_stopped(paths)


def test_nested_timeout_cannot_signal_enclosing_runtime_or_host_group(tmp_path):
    with nested_vad_process(tmp_path, inner_timeout=2, catch_timeout=True) as (command, paths):
        result = run_process(command, timeout=8)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "inner timeout handled\nruntime survived\n"
        assert_stopped(paths)


def test_cancellation_does_not_resignal_reaped_group_or_mask_deadline(monkeypatch):
    import os
    import signal

    killpg = os.killpg
    killed = set()

    def signal_once(group, sig):
        assert group != os.getpgrp()
        if sig == signal.SIGKILL:
            if group in killed:
                # macOS can report EPERM once only zombies remain in a group.
                raise PermissionError("synthetic zombies-only process group")
            killed.add(group)
        killpg(group, sig)

    monkeypatch.setattr(os, "killpg", signal_once)
    with pytest.raises(ProcessDeadlineExceeded):
        run_process([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.5)
    assert len(killed) == 1


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_deadlines_do_not_spawn(timeout):
    with pytest.raises(ValueError):
        run_process(["not-a-real-binary"], timeout=timeout)


def test_converter_timeout_preserves_output_and_skips_raw_fallback(monkeypatch, tmp_path):
    from localplaud.worker import convert

    source, output = tmp_path / "raw.opus", tmp_path / "output.wav"
    source.write_bytes((bytes([0xB8]) + bytes(79)) * 50)
    output.write_bytes(b"existing output")
    monkeypatch.setattr(convert, "ffmpeg_available", lambda: True)
    calls = []

    def timeout(*args, **kwargs):
        calls.append(1)
        raise ProcessDeadlineExceeded(1, b"", b"private fixture")

    monkeypatch.setattr(convert, "run_process", timeout)
    with pytest.raises(convert.ConversionTimeout):
        convert.to_wav(source, output)
    assert calls == [1]
    assert output.read_bytes() == b"existing output"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["output.wav", "raw.opus"]


def test_vad_timeout_removes_partial_region(monkeypatch, tmp_path):
    from localplaud.asr import vad

    output = tmp_path / "region.wav"
    monkeypatch.setattr(vad.shutil, "which", lambda _: "ffmpeg")

    def timeout(args, **kwargs):
        Path(args[-1]).write_bytes(b"partial region")
        assert kwargs["timeout"] == 120
        raise ProcessDeadlineExceeded(120, b"", b"")

    monkeypatch.setattr(vad, "run_process", timeout)
    with pytest.raises(vad.VadError, match="timed out"):
        vad.slice_region("synthetic.wav", 0, 1, output)
    assert not output.exists()


def test_vad_cancellation_removes_partial_region_and_refuses_source_overwrite(monkeypatch, tmp_path):
    from localplaud.asr import vad

    source, output = tmp_path / "source.wav", tmp_path / "region.wav"
    source.write_bytes(b"original synthetic audio")
    monkeypatch.setattr(vad.shutil, "which", lambda _: "ffmpeg")

    def cancel(args, **kwargs):
        Path(args[-1]).write_bytes(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(vad, "run_process", cancel)
    with pytest.raises(KeyboardInterrupt):
        vad.slice_region(source, 0, 1, output)
    assert not output.exists()
    with pytest.raises(vad.VadError, match="must differ"):
        vad.slice_region(source, 0, 1, source)
    assert source.read_bytes() == b"original synthetic audio"


def test_whisper_deadline_and_failure_do_not_expose_decoded_text(monkeypatch, tmp_path):
    from localplaud.asr import whispercpp_provider as whisper
    from localplaud.asr.base import AsrError
    from localplaud.config import AsrConfig

    model = tmp_path / "fake-model.bin"
    model.touch()
    cfg = AsrConfig(whispercpp={"model_path": model, "timeout_seconds": 7})
    monkeypatch.setattr(whisper.shutil, "which", lambda _: "whisper-cli")

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 7
        raise ProcessDeadlineExceeded(7, b"", b"private fixture")

    monkeypatch.setattr(whisper, "run_process", timeout)
    with pytest.raises(AsrError, match="timed out") as failure:
        whisper.WhisperCppProvider(cfg).transcribe(Path("synthetic.wav"))
    assert "private fixture" not in str(failure.value)
    monkeypatch.setattr(
        whisper, "run_process", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "private fixture")
    )
    with pytest.raises(AsrError) as failure:
        whisper.WhisperCppProvider(cfg).transcribe(Path("synthetic.wav"))
    assert "private fixture" not in str(failure.value)
