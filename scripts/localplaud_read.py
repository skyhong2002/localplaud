"""Read localplaud through a local account and revocable session, without Google.

Credentials are loaded from a private JSON file, never command-line passwords.
"""

from __future__ import annotations

import argparse
import fcntl
import http.cookiejar
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class ReadError(RuntimeError):
    pass


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urllib.parse.urlsplit(newurl)
        if (destination.scheme, destination.netloc) != self.origin:
            raise ReadError("Refused a redirect outside the configured localplaud site")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Reader:
    def __init__(self, credentials: Path):
        config = json.loads(credentials.read_text())
        self.base = config.get("base_url", "https://plaud.observe.tw").rstrip("/")
        url = urllib.parse.urlsplit(self.base)
        if (
            not url.hostname or url.username or url.password or url.path or url.query or url.fragment
            or (url.scheme != "https" and not (
                url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}
            ))
        ):
            raise ReadError("Use the HTTPS localplaud origin (HTTP is allowed only on loopback)")
        self.username, self.password = config["username"], config["password"]
        if not isinstance(self.username, str) or not isinstance(self.password, str):
            raise ReadError("Invalid account credential file")
        self.cookie_path = credentials.with_suffix(".cookies")
        self.cookies = http.cookiejar.MozillaCookieJar()
        if self.cookie_path.exists():
            self.cookies.load(str(self.cookie_path), ignore_discard=True)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies),
            SameOriginRedirect((url.scheme, url.netloc)),
        )

    def login(self):
        self.cookies.clear()
        body = urllib.parse.urlencode({
            "identifier": self.username, "password": self.password, "next": "/account",
        }).encode()
        request = urllib.request.Request(
            self.base + "/login", data=body,
            headers={"Origin": self.base, "User-Agent": "localplaud-hermes-reader/1"},
        )
        with self.opener.open(request, timeout=30) as response:
            response.read()
        if not any(cookie.name == "localplaud_session" for cookie in self.cookies):
            raise ReadError("Account login failed; check credentials and account status")
        fd, name = tempfile.mkstemp(prefix=".localplaud-session-", dir=self.cookie_path.parent)
        os.close(fd)
        try:
            self.cookies.save(name, ignore_discard=True)
            os.chmod(name, 0o600)
            os.replace(name, self.cookie_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def read(self, path):
        for attempt in range(2):
            request = urllib.request.Request(self.base + path, headers={
                "Accept": "application/json, text/plain, text/markdown",
                "User-Agent": "localplaud-hermes-reader/1",
            })
            try:
                with self.opener.open(request, timeout=30) as response:
                    if response.headers.get_content_type() == "text/html":
                        raise ReadError("Expected recording data, received an HTML page")
                    return response.read().decode("utf-8")
            except urllib.error.HTTPError as error:
                if error.code == 401 and attempt == 0:
                    self.login()
                    continue
                if error.code == 403:
                    raise ReadError("Account is pending approval or lacks read permission") from None
                raise ReadError(f"Recording request failed (HTTP {error.code})") from None
        raise ReadError("Account session could not be established")

    def run(self, command, file_id=None):
        if command in {"check", "list"}:
            result = self.read("/api/files")
            if command == "check":
                return json.dumps({"ok": True, "recordings": len(json.loads(result)["files"])})
            return result
        if not file_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", file_id):
            raise ReadError("Provide a valid recording ID")
        suffix = {"transcript": "export/transcript.txt", "notes": "export/notes.md", "recording": "export.md"}[command]
        return self.read(f"/file/{file_id}/{suffix}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, default=Path(os.environ.get(
        "LOCALPLAUD_ACCOUNT_FILE", str(Path.home() / ".hermes/localplaud_account.json")
    )))
    parser.add_argument("command", choices=["check", "list", "transcript", "notes", "recording"])
    parser.add_argument("file_id", nargs="?")
    args = parser.parse_args()
    try:
        # Concurrent tool calls share a session instead of racing logins/revocations.
        with args.credentials.with_suffix(".lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = Reader(args.credentials).run(args.command, args.file_id)
        print(result)
    except (ReadError, OSError, ValueError, KeyError, urllib.error.URLError) as error:
        message = str(error) if isinstance(error, ReadError) else "Check connection and private credential files"
        print(f"Localplaud read failed: {message}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
