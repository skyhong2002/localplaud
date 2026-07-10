"""Tests for the Plaud client's download + cloud-artifact paths (mocked HTTP)."""

from __future__ import annotations

import gzip

import httpx
import respx

from localplaud.config import PlaudConfig
from localplaud.plaud.client import PlaudClient, _ext_from_url, _find_url
from localplaud.plaud.models import PlaudFileDTO

API = "https://api-apse1.plaud.ai"


def _cfg() -> PlaudConfig:
    return PlaudConfig(api_base=API, token="Bearer testtoken")


def test_find_url_prefers_match():
    obj = {"status": 0, "data": {"link": "https://cdn/other", "url": "https://x.s3.amazonaws.com/a.mp3?Signature=z"}}
    assert _find_url(obj, must_contain=("amazonaws",)).endswith("Signature=z")


def test_ext_from_url():
    assert _ext_from_url("https://h/audiofiles/ID.mp3?Signature=z") == "mp3"
    assert _ext_from_url("https://h/audiofiles/ID?Signature=z", default="mp3") == "mp3"


def test_find_url_strict_by_default():
    obj = {"a": "https://cdn.example/other.png", "b": "https://x/trans_result.json.gz?s=1"}
    # No match for the requested asset -> None (avoid fetching the wrong asset).
    assert _find_url(obj, must_contain=("ai_content",)) is None
    # Explicit opt-in fallback returns the first URL.
    assert _find_url(obj, must_contain=("ai_content",), allow_any=True).endswith(".png")
    # A real match wins.
    assert "trans_result" in _find_url(obj, must_contain=("trans_result",))


def _no_ssrf(monkeypatch):
    # These tests exercise fetch/parse logic, not the SSRF guard (which does
    # real DNS). The guard has its own dedicated tests below.
    monkeypatch.setattr("localplaud.plaud.client._assert_safe_fetch_url", lambda u: None)


@respx.mock
def test_download_audio_uses_temp_url_and_mp3_ext(tmp_path, monkeypatch):
    _no_ssrf(monkeypatch)
    fid = "abc123"
    signed = f"https://apse1-prod-plaud-bucket.s3.amazonaws.com/audiofiles/{fid}.mp3?AWSAccessKeyId=k&Signature=s&Expires=1"
    respx.get(f"{API}/file/temp-url/{fid}").mock(
        return_value=httpx.Response(200, json={"status": 0, "data": {"url": signed}})
    )
    respx.get(url__regex=r".*audiofiles/abc123\.mp3.*").mock(
        return_value=httpx.Response(200, content=b"ID3fakeaudio")
    )
    dto = PlaudFileDTO(id=fid, fullname=f"{fid}.opus")
    with PlaudClient(_cfg()) as c:
        dest = c.download_audio(dto, tmp_path)
    assert dest.name == "audio.mp3"  # from the URL, not the .opus fullname
    assert dest.read_bytes() == b"ID3fakeaudio"


@respx.mock
def test_get_cloud_summary_md_gunzips(tmp_path, monkeypatch):
    _no_ssrf(monkeypatch)
    fid = "abc"
    asset = f"https://apse1-prod-plaud-content-storage.s3.amazonaws.com/permanent/w/m/file_summary/{fid}/ai_content.md.gz?Signature=z"
    detail = {"status": 0, "data": {"assets": {"summary": asset}}}
    respx.get(f"{API}/file/detail/{fid}").mock(return_value=httpx.Response(200, json=detail))
    respx.get(url__regex=r".*ai_content\.md\.gz.*").mock(
        return_value=httpx.Response(200, content=gzip.compress(b"# Title\n\nhello"))
    )
    with PlaudClient(_cfg()) as c:
        md = c.get_cloud_summary_md(fid)
    assert md.startswith("# Title")


def test_assert_safe_fetch_url_rejects_bad(monkeypatch):
    import pytest

    from localplaud.plaud.client import PlaudError, _assert_safe_fetch_url

    # non-https rejected before any DNS
    with pytest.raises(PlaudError):
        _assert_safe_fetch_url("http://example.com/x")

    # a host resolving to a private/loopback IP is rejected
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 443))],
    )
    with pytest.raises(PlaudError):
        _assert_safe_fetch_url("https://metadata.internal/x")

    # a public IP passes
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("52.216.1.1", 443))]
    )
    _assert_safe_fetch_url("https://s3.amazonaws.com/x")  # no raise


def test_bounded_gunzip_blocks_bomb():
    import pytest

    from localplaud.plaud.client import PlaudError, _bounded_gunzip

    assert _bounded_gunzip(gzip.compress(b"hello"), 1000) == b"hello"
    bomb = gzip.compress(b"\x00" * (5 * 1024 * 1024))
    with pytest.raises(PlaudError):
        _bounded_gunzip(bomb, 1024)


@respx.mock
def test_list_files_parses_wrapper():
    payload = {
        "status": 0,
        "msg": "success",
        "data_file_total": 1,
        "data_file_list": [{"id": "f1", "filename": "rec", "fullname": "f1.opus", "is_trans": True}],
    }
    respx.get(f"{API}/file/simple/web").mock(return_value=httpx.Response(200, json=payload))
    with PlaudClient(_cfg()) as c:
        resp = c.list_files(limit=5)
    assert resp.data_file_total == 1
    assert resp.data_file_list[0].id == "f1"
    assert resp.data_file_list[0].is_trans is True
