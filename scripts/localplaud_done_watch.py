"""Notify when Local Plaud recordings newly transition to status=done.

First successful run establishes a baseline and stays silent. Subsequent runs
print a Discord-ready message only when one or more recordings become done.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DEFAULT_BASE_URL = "https://plaud.observe.tw"
BASE_URL = os.environ.get("LOCALPLAUD_URL", DEFAULT_BASE_URL).rstrip(chr(47))
TOKEN_FILE = Path(
    os.environ.get("LOCALPLAUD_TOKEN_FILE", str(HERMES_HOME / "localplaud_api_token"))
)
STATE_FILE = Path(
    os.environ.get(
        "LOCALPLAUD_WATCH_STATE",
        str(HERMES_HOME / "cron/state/localplaud_done_watch.json"),
    )
)
DISPLAY_TIMEZONE = ZoneInfo("Asia/Taipei")
REQUEST_TIMEOUT = 10
FETCH_ATTEMPTS = 2
FAILURE_THRESHOLD = 3
ALERT_INTERVAL = 3600


class SourceUnavailable(RuntimeError):
    def __init__(self, reason: str, *, transient: bool = True):
        super().__init__(reason)
        self.transient = transient


def escape_markdown_link_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def format_duration(duration_ms: object) -> str | None:
    try:
        total_seconds = max(0, int(duration_ms) // 1000)
    except (TypeError, ValueError):
        return None
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def format_started_at(start_time_ms: object) -> str | None:
    try:
        timestamp = int(start_time_ms) / 1000
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=DISPLAY_TIMEZONE).strftime("%Y/%m/%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return None


def fetch_files() -> list[dict]:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SourceUnavailable("API 權杖檔無法讀取", transient=False) from exc
    if not token:
        raise SourceUnavailable("API 權杖檔是空的", transient=False)
    request = urllib.request.Request(
        f"{BASE_URL}/api/files",
        headers={"X-Auth-Token": token, "Accept": "application/json"},
    )
    for attempt in range(FETCH_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = json.load(response)
            files = payload.get("files") if isinstance(payload, dict) else None
            if not isinstance(files, list) or any(
                not isinstance(item, dict) or not item.get("id") for item in files
            ):
                raise SourceUnavailable("錄音清單格式異常", transient=False)
            return files
        except urllib.error.HTTPError as exc:
            error = SourceUnavailable(
                f"HTTP {exc.code}", transient=exc.code in {408, 429, 500, 502, 503, 504}
            )
        except (OSError, TimeoutError, urllib.error.URLError):
            error = SourceUnavailable("連線失敗或逾時")
        except (ValueError, UnicodeError) as exc:
            raise SourceUnavailable("錄音清單不是有效 JSON", transient=False) from exc
        if not error.transient or attempt + 1 == FETCH_ATTEMPTS:
            raise error
        time.sleep(1)
    raise AssertionError("unreachable")


def load_state() -> dict[str, str]:
    if not STATE_FILE.exists():
        return {}
    payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    statuses = payload.get("statuses") if isinstance(payload, dict) else None
    if not isinstance(statuses, dict):
        raise ValueError("Invalid completion notification state")
    return {str(k): str(v) for k, v in statuses.items()}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".localplaud-watch-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_state(statuses: dict[str, str]) -> None:
    write_json(STATE_FILE, {"statuses": statuses})


def health_path() -> Path:
    return STATE_FILE.with_suffix(".health.json")


def source_failure(error: SourceUnavailable) -> None:
    path = health_path()
    try:
        health = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        health = {}
    if not isinstance(health, dict):
        health = {}
    now = time.time()
    count = int(health.get("consecutive_failures", 0)) + 1
    threshold = FAILURE_THRESHOLD if error.transient else 1
    last_alert = health.get("last_alert_at")
    alert = count >= threshold and (last_alert is None or now - float(last_alert) >= ALERT_INTERVAL)
    health.update(consecutive_failures=count, last_failure_at=now, last_error=str(error))
    if alert:
        health["last_alert_at"] = now
    write_json(path, health)
    # Expected dependency failures are diagnostics, not model-provider failures.
    # Empty stdout makes Hermes no_agent cron silent during brief interruptions.
    print(f"Local Plaud poll deferred: {error}; consecutive failures={count}", file=sys.stderr)
    if alert:
        print(
            f"⚠️ Local Plaud 通知暫時無法讀取錄音清單（{error}）。"
            "通知紀錄已保留，恢復連線後會接續檢查。"
        )


def poll() -> None:
    first_run = not STATE_FILE.exists()
    try:
        previous = load_state()
    except (OSError, ValueError):
        source_failure(SourceUnavailable("通知紀錄無法讀取，請檢查狀態檔", transient=False))
        return
    try:
        files = fetch_files()
    except SourceUnavailable as error:
        source_failure(error)
        return
    current = {str(item["id"]): str(item.get("status", "")) for item in files}

    completed = (
        []
        if first_run
        else [
            item
            for item in files
            if item.get("status") == "done" and previous.get(str(item["id"])) != "done"
        ]
    )
    save_state(current)
    write_json(health_path(), {"consecutive_failures": 0, "last_success_at": time.time()})

    if not completed:
        return

    if len(completed) == 1:
        print("🎙️ Local Plaud 新記錄已處理完成")
    else:
        print(f"🎙️ Local Plaud 有 {len(completed)} 筆新記錄已處理完成")

    for item in completed:
        title = str(item.get("filename") or item.get("cloud_filename") or "未命名記錄")
        file_id = urllib.parse.quote(str(item["id"]), safe="")
        print(f"[{escape_markdown_link_text(title)}]({BASE_URL}/file/{file_id})")
        metadata = [
            value
            for value in (
                format_started_at(item.get("start_time_ms")),
                format_duration(item.get("duration_ms")),
            )
            if value
        ]
        if metadata:
            print(" · ".join(metadata))


def main() -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_FILE.with_suffix(".lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        poll()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Read API only; do not notify or update state"
    )
    args = parser.parse_args()
    if args.check:
        try:
            print(json.dumps({"ok": True, "recordings": len(fetch_files())}))
        except SourceUnavailable as error:
            print(f"Local Plaud check failed: {error}", file=sys.stderr)
            sys.exit(1)
    else:
        main()
