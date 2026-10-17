"""Verify the real app rejects uploads before parsing, behind authentication."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import localplaud.api.app as web
from localplaud.api.upload_limits import MAX_UPLOAD_BYTES, MULTIPART_OVERHEAD_BYTES


@pytest.mark.parametrize("authorized, expected", [(False, 401), (True, 413)])
def test_auth_gate_and_streaming_limiter_do_not_consume_declared_oversize_body(
    monkeypatch, authorized, expected
):
    settings = SimpleNamespace(api=SimpleNamespace(
        accounts_enabled=False, auth_token="fixture-only-token", login_password=None,
        session_secret=None,
    ))
    monkeypatch.setattr(web, "get_settings", lambda: settings)

    def body():
        raise AssertionError("rejected upload must not be read or spooled")
        yield b"unreachable"

    headers = {"Content-Length": str(MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES + 1),
               "Content-Type": "multipart/form-data; boundary=fixture"}
    if authorized:
        headers["Authorization"] = "Bearer fixture-only-token"
    # No lifespan: never start a daemon, load account state, or resume jobs.
    response = TestClient(web.app).post("/api/imports/local/audio", headers=headers, content=body())
    assert response.status_code == expected
