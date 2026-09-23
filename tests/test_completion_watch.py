"""The external watcher must preserve completion state through source outages."""

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/localplaud_done_watch.py"
spec = importlib.util.spec_from_file_location("completion_watch", SCRIPT)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(watch, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(watch, "TOKEN_FILE", tmp_path / "token")
    monkeypatch.setattr(watch.time, "sleep", lambda _: None)
    watch.TOKEN_FILE.write_text("test-token")
    state = {
        "status": 200,
        "calls": 0,
        "files": [{"id": "one", "status": "processing", "filename": "Example"}],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/api/files"
            assert self.headers["X-Auth-Token"] == "test-token"
            state["calls"] += 1
            status = state["status"]
            if isinstance(status, list):
                status = status.pop(0)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(state.get("payload", {"files": state["files"]})).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    monkeypatch.setattr(watch, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield state
    server.shutdown()
    server.server_close()


def poll():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        watch.main()
    return output.getvalue()


def test_initial_baseline_and_single_completion(api):
    api["files"].append({"id": "old", "status": "done", "filename": "Old"})
    assert poll() == ""
    api["files"][0]["status"] = "done"
    result = poll()
    assert "Example" in result and "Old" not in result
    assert poll() == ""


def test_short_outage_preserves_state_and_recovery(api):
    poll()
    before = watch.STATE_FILE.read_bytes()
    api["status"] = 502
    assert poll() == ""
    assert poll() == ""
    assert watch.STATE_FILE.read_bytes() == before
    api["status"] = 200
    api["files"][0]["status"] = "done"
    assert "Example" in poll()
    assert poll() == ""
    assert json.loads(watch.health_path().read_text())["consecutive_failures"] == 0


def test_first_failure_does_not_baseline_existing_done_records(api):
    api["status"] = 503
    assert poll() == ""
    assert not watch.STATE_FILE.exists()
    api["status"] = 200
    api["files"][0]["status"] = "done"
    assert poll() == ""


def test_transient_retry_recovers_in_same_run(api):
    api["status"] = [502, 200]
    assert poll() == ""
    assert api["calls"] == 2
    assert watch.STATE_FILE.exists()


def test_persistent_failure_alerts_are_throttled(api, monkeypatch):
    poll()
    api["status"] = 503
    clock = [10000.0]
    monkeypatch.setattr(watch.time, "time", lambda: clock[0])
    assert poll() == ""
    assert poll() == ""
    assert "HTTP 503" in poll()
    assert poll() == ""
    clock[0] += 3600
    assert "HTTP 503" in poll()
    api["status"] = 200
    assert poll() == ""
    api["status"] = 502
    assert poll() == ""


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_alert_immediately_without_retry(api, status):
    api["status"] = status
    assert f"HTTP {status}" in poll()
    assert api["calls"] == 1
    assert not watch.STATE_FILE.exists()
    assert poll() == ""


def test_timeout_is_a_source_failure_without_model_fallback(api, monkeypatch):
    poll()
    before = watch.STATE_FILE.read_bytes()

    def timeout(*_args, **_kwargs):
        raise TimeoutError("should not expose request internals")

    monkeypatch.setattr(watch.urllib.request, "urlopen", timeout)
    assert poll() == ""
    assert watch.STATE_FILE.read_bytes() == before
    health = json.loads(watch.health_path().read_text())
    assert health["last_error"] == "連線失敗或逾時"


@pytest.mark.parametrize("payload", [{}, {"files": None}, {"files": [{}]}])
def test_malformed_response_preserves_baseline(api, payload):
    poll()
    before = watch.STATE_FILE.read_bytes()
    api["payload"] = payload
    assert "格式異常" in poll()
    assert watch.STATE_FILE.read_bytes() == before


def test_corrupt_state_is_not_reset(api):
    watch.STATE_FILE.write_text("bad-json")
    assert "通知紀錄無法讀取" in poll()
    assert watch.STATE_FILE.read_text() == "bad-json"
    assert api["calls"] == 0


def test_concurrent_poll_does_not_duplicate_state_transition(api):
    with watch.STATE_FILE.with_suffix(".lock").open("a") as lock:
        watch.fcntl.flock(lock, watch.fcntl.LOCK_EX | watch.fcntl.LOCK_NB)
        assert poll() == ""
        assert api["calls"] == 0


def test_check_does_not_mutate_state_or_print_recordings(api, monkeypatch):
    import os

    api["files"][0]["status"] = "done"
    env = dict(
        os.environ,
        LOCALPLAUD_URL=watch.BASE_URL,
        LOCALPLAUD_TOKEN_FILE=str(watch.TOKEN_FILE),
        LOCALPLAUD_WATCH_STATE=str(watch.STATE_FILE),
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"ok": True, "recordings": 1}
    assert not watch.STATE_FILE.exists()
    assert not watch.health_path().exists()


def test_invalid_time_cannot_break_notification(api):
    poll()
    api["files"][0].update(status="done", start_time_ms=10**30)
    assert "Example" in poll()
