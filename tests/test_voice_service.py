"""Watch scans release their allocations by exiting a separate process."""

import json
import subprocess
from unittest.mock import Mock

import pytest

from localplaud import voice_service as service


@pytest.fixture
def main_env(tmp_path, monkeypatch):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"enabled": True}))
    monkeypatch.setattr(service, "get_engine", Mock())
    monkeypatch.setattr(service, "create_schema", Mock())
    monkeypatch.setattr(service.os, "umask", Mock())
    monkeypatch.setattr(service.time, "monotonic", lambda: 100)
    monkeypatch.setattr(service, "library_fingerprint", Mock(return_value="unchanged"))
    monkeypatch.setattr(service, "VoiceWorker", Mock())
    monkeypatch.setattr(service, "scan", Mock(return_value={"counts": {}}))
    monkeypatch.setattr(service.subprocess, "run", Mock())
    return profile


def watch(monkeypatch, profile, *options, cycles=2):
    monkeypatch.setattr(
        service.sys, "argv", ["voice_service", "--profile", str(profile), "--watch", *options]
    )
    sleeps = Mock(side_effect=[None] * (cycles - 1) + [KeyboardInterrupt])
    stop = service.ServiceSignals()
    monkeypatch.setattr(stop, "wait", sleeps)
    monkeypatch.setattr(service, "ServiceSignals", lambda: stop)
    with pytest.raises(KeyboardInterrupt):
        service.main()
    assert sleeps.call_args_list == [((300,),)] * cycles


@pytest.mark.parametrize("limit", [None, 3])
def test_watch_due_child_receipt_caches_fingerprint(main_env, monkeypatch, limit):
    def run(command, *, check, start_new_session):
        assert start_new_session is True
        assert check is True
        # The parent retains its lock throughout the child pass.
        with open(main_env.parent / "voice-service.lock") as lock:
            with pytest.raises(BlockingIOError):
                service.fcntl.flock(lock, service.fcntl.LOCK_EX | service.fcntl.LOCK_NB)
        service.report(main_env.parent / "receipt.json", {"counts": {"recordings_extracted": 1}})

    service.subprocess.run.side_effect = run
    options = [] if limit is None else ["--limit", str(limit)]
    watch(monkeypatch, main_env, *options)
    service.subprocess.run.assert_called_once_with(
        [
            service.sys.executable,
            "-m",
            "localplaud.voice_service",
            "--profile",
            str(main_env),
            *options,
            "--child-pass",
        ],
        check=True,
        start_new_session=True,
    )
    assert service.library_fingerprint.call_count == 2
    service.VoiceWorker.assert_not_called()
    service.scan.assert_not_called()


@pytest.mark.parametrize("returncode", [1, -9])
def test_failed_child_retries_without_caching(main_env, monkeypatch, capsys, returncode):
    service.report(main_env.parent / "receipt.json", {"counts": {}})
    service.subprocess.run.side_effect = subprocess.CalledProcessError(returncode, "child")
    watch(monkeypatch, main_env)
    assert service.subprocess.run.call_count == 2
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [
        {"event": "scan_failed", "error": "CalledProcessError"}
    ] * 2


def test_capped_child_receipt_retries_until_below_limit(main_env, monkeypatch):
    counts = iter([3, 1])

    def run(*args, **kwargs):
        service.report(
            main_env.parent / "receipt.json", {"counts": {"recordings_extracted": next(counts)}}
        )

    service.subprocess.run.side_effect = run
    watch(monkeypatch, main_env, "--limit", "3", cycles=3)
    assert service.subprocess.run.call_count == 2


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_single_pass_in_process_and_child_skips_lock(main_env, monkeypatch, child, fails):
    monkeypatch.setattr(
        service.sys,
        "argv",
        ["voice_service", "--profile", str(main_env), "--limit", "2"]
        + (["--child-pass"] if child else []),
    )
    flock = Mock()
    monkeypatch.setattr(service.fcntl, "flock", flock)
    if fails:
        service.scan.side_effect = RuntimeError("scan failed")
        with pytest.raises(RuntimeError):
            service.main()
    else:
        service.main()
    assert flock.call_count == (0 if child else 1)
    service.VoiceWorker.assert_called_once_with({"enabled": True}, main_env.parent / "runtime.log")
    service.scan.assert_called_once_with(
        {"enabled": True},
        service.VoiceWorker.return_value,
        main_env.parent / "receipt.json",
        limit=2,
    )
    service.VoiceWorker.return_value.close.assert_called_once_with()
    service.subprocess.run.assert_not_called()
