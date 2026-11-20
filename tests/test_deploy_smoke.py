import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy/smoke-test.sh"
ORIGIN = "https://app.test"
FINGERPRINT = "ab" * 32
ASSET_LINKS = "/.well-known/assetlinks.json"


@pytest.fixture
def run_smoke(tmp_path):
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys, urllib.parse\n"
        "args = sys.argv[1:]\n"
        "path = urllib.parse.urlsplit(args[-1]).path\n"
        "code, headers, body = json.loads(os.environ['REPLIES'])[path]\n"
        "pathlib.Path(args[args.index('-D') + 1]).write_text(headers)\n"
        "pathlib.Path(args[args.index('-o') + 1]).write_text(body)\n"
        "print(code, end='')\n"
    )
    curl.chmod(0o755)

    def run(replies, *args):
        env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", REPLIES=json.dumps(replies))
        return subprocess.run(["bash", str(SCRIPT), ORIGIN, *args], env=env, capture_output=True, text=True)

    return run


@pytest.mark.parametrize(
    ("root_code", "root_headers", "login_code", "expected"),
    [
        (200, "", 404, 0),
        (401, "", 404, 0),
        (303, "Location: /login?next=%2F\r\n", 200, 0),
        (303, "Location: /login?next=%2F\r\n", 500, 1),
        (303, "Location: /elsewhere\r\n", 200, 1),
        (503, "", 200, 1),
    ],
)
def test_smoke_distinguishes_private_root_from_broken_service(
    run_smoke, root_code, root_headers, login_code, expected
):
    result = run_smoke({"/healthz": [200, "", ""], "/": [root_code, root_headers, ""], "/login": [login_code, "", ""]})
    assert result.returncode == expected


@pytest.mark.parametrize(
    ("code", "content_type", "package_name", "digest", "expected"),
    [
        (200, "application/json; charset=utf-8", "org.localplaud.android", ":".join(["AB"] * 32), 0),
        (200, "text/html", "org.localplaud.android", FINGERPRINT, 1),
        (200, "application/json", "another.app", FINGERPRINT, 1),
        (200, "application/json", "org.localplaud.android", "cd" * 32, 1),
        (303, "application/json", "org.localplaud.android", FINGERPRINT, 1),
        (401, "application/json", "org.localplaud.android", FINGERPRINT, 1),
    ],
)
def test_android_smoke_requires_public_matching_statement(
    run_smoke, code, content_type, package_name, digest, expected
):
    statement = {
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": package_name,
            "sha256_cert_fingerprints": [digest],
        },
    }
    replies = {
        "/healthz": [200, "", ""],
        "/": [401, "", ""],
        ASSET_LINKS: [code, f"Content-Type: {content_type}\r\n", json.dumps([statement])],
    }
    result = run_smoke(replies, FINGERPRINT)
    assert result.returncode == expected, result.stdout + result.stderr
