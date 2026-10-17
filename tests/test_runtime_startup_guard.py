"""Run with tests/run_runtime_offline.sh, before any real provider can load."""

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    not getattr(sys, "_localplaud_runtime_guard", False),
    reason="requires interpreter-startup isolation: use tests/run_runtime_offline.sh",
)


def test_startup_guard_blocks_parent_and_child_network_and_env_loading(tmp_path):
    import socket

    from localplaud.config import Settings, load_dotenv

    assert Settings.model_config["env_file"] is None
    assert load_dotenv() is False
    with pytest.raises(RuntimeError, match="RUNTIME_TEST_OUTBOUND_NETWORK_BLOCKED"):
        socket.socket(socket.AF_INET)
    (tmp_path / ".env").write_text("LOCALPLAUD_PIPELINE__CONCURRENCY=99\n")
    child = subprocess.run([
        sys.executable, "-c",
        "from localplaud.config import Settings, load_dotenv; "
        "assert Settings.model_config['env_file'] is None; "
        "assert load_dotenv() is False; assert Settings().pipeline.concurrency != 99; "
        "import socket; socket.socket(socket.AF_INET6)",
    ], cwd=tmp_path, capture_output=True, text=True, timeout=5)
    assert child.returncode != 0
    assert "RUNTIME_TEST_OUTBOUND_NETWORK_BLOCKED" in child.stderr


def test_startup_guard_blocks_real_model_import_in_child():
    child = subprocess.run(
        [sys.executable, "-c", "import torch"], capture_output=True, text=True, timeout=5
    )
    assert child.returncode != 0
    assert "RUNTIME_TEST_REAL_MODEL_IMPORT_BLOCKED" in child.stderr


def test_stale_group_marker_never_transfers_host_group_ownership(monkeypatch):
    from localplaud.subprocesses import run_process

    monkeypatch.setenv("_LOCALPLAUD_SUBPROCESS_GROUP", "parent:0")
    result = run_process(
        [sys.executable, "-c", "import os; print(os.getpid(), os.getpgrp())"], timeout=3
    )
    pid, group = map(int, result.stdout.split())
    assert pid == group and group != os.getpgrp()
