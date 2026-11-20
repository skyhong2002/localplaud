import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from localplaud.api.app import app
from localplaud.config import ApiConfig, Settings

ASSET_LINKS = "/.well-known/assetlinks.json"


@pytest.mark.parametrize("auth_mode", ["accounts", "password", "token"])
def test_android_asset_links_do_not_require_or_disable_workspace_auth(monkeypatch, auth_mode):
    monkeypatch.setenv("LOCALPLAUD_API__SESSION_SECRET", "a-test-only-session-secret")
    if auth_mode == "accounts":
        monkeypatch.setenv("LOCALPLAUD_API__ACCOUNTS_ENABLED", "true")
    elif auth_mode == "password":
        monkeypatch.setenv("LOCALPLAUD_API__LOGIN_PASSWORD", "private-test-password")
    else:
        monkeypatch.setenv("LOCALPLAUD_API__AUTH_TOKEN", "private-test-token")
    monkeypatch.setenv("LOCALPLAUD_API__ANDROID_SIGNING_FINGERPRINTS", json.dumps(["ab" * 32]))
    settings = Settings()
    monkeypatch.setattr("localplaud.config._settings", settings)
    client = TestClient(app, base_url="https://testserver")

    response = client.get(ASSET_LINKS, follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == [{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": "org.localplaud.android",
            "sha256_cert_fingerprints": [":".join(["AB"] * 32)],
        },
    }]
    assert client.post(ASSET_LINKS, headers={"Origin": "https://testserver"}).status_code == 405
    assert client.get("/api/files", follow_redirects=False).status_code == 401


def test_android_asset_links_are_disabled_without_signing_fingerprints(monkeypatch):
    settings = Settings(api=ApiConfig(auth_token="private-test-token"))
    monkeypatch.setattr("localplaud.config._settings", settings)
    assert TestClient(app).get(ASSET_LINKS, follow_redirects=False).status_code == 404


def test_android_fingerprints_are_validated_and_deduplicated():
    formatted = ":".join(["AB"] * 32)
    assert ApiConfig(android_signing_fingerprints=["ab" * 32, formatted]).android_signing_fingerprints == [formatted]
    with pytest.raises(ValidationError, match="must be SHA-256 hex digests"):
        ApiConfig(android_signing_fingerprints=["not-a-digest"])
