"""Tests for Plaud auth: cURL parsing and header assembly."""

from __future__ import annotations

import pytest

from localplaud.config import PlaudConfig
from localplaud.plaud.auth import AuthError, build_headers, parse_curl

CHROME_CURL = """\
curl 'https://api-apse1.plaud.ai/user/me' \
  -H 'authorization: Bearer eyJhbGci.SAMPLE.TOK' \
  -H 'x-device-id: web-abc123' \
  -H 'x-pld-user: user-xyz' \
  -H 'app-platform: web' \
  -H 'origin: https://web.plaud.ai' \
  -b '_ga=GA1.1.1; AWSALBTG=xxx'
"""


def test_parse_curl_lowercase_headers_preserves_token():
    p = parse_curl(CHROME_CURL)
    assert p["api_base"] == "https://api-apse1.plaud.ai"
    assert p["token"] == "Bearer eyJhbGci.SAMPLE.TOK"
    assert p["cookie"] == "_ga=GA1.1.1; AWSALBTG=xxx"
    # Plaud client headers kept; browser noise dropped.
    assert p["headers"]["x-device-id"] == "web-abc123"
    assert "origin" not in p["headers"]
    assert "authorization" not in {k.lower() for k in p["headers"]}


def test_build_headers_from_parsed_curl():
    p = parse_curl(CHROME_CURL)
    cfg = PlaudConfig(api_base=p["api_base"], token=p["token"], extra_headers=p["headers"])
    h = build_headers(cfg)
    assert h["Authorization"] == "Bearer eyJhbGci.SAMPLE.TOK"
    assert h["x-device-id"] == "web-abc123"


def test_build_headers_bare_token_gets_bearer_prefix():
    h = build_headers(PlaudConfig(token="rawtokenvalue"))
    assert h["Authorization"] == "Bearer rawtokenvalue"


def test_build_headers_requires_credentials():
    with pytest.raises(AuthError):
        build_headers(PlaudConfig())
