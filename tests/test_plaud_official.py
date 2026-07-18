"""Tests for the official Open API client + OAuth token store (mocked HTTP)."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from localplaud.config import PlaudOfficialConfig
from localplaud.plaud.common import PlaudAuthError
from localplaud.plaud.oauth import (
    OAuthError,
    OfficialTokenStore,
    create_authorization_request,
    exchange_authorization_code,
    native_login,
    run_loopback_callback,
)
from localplaud.plaud.official import PlaudOfficialClient, _parse_iso_ms, _to_dto

API = "https://platform.plaud.ai/developer/api"
REFRESH_URL = f"{API}/oauth/third-party/access-token/refresh"


def _write_tokens(path, access="tok-live", refresh="ref-1", expires_in_ms=3_600_000):
    path.write_text(
        json.dumps(
            {
                "access_token": access,
                "refresh_token": refresh,
                "token_type": "bearer",
                "expires_at": int(time.time() * 1000) + expires_in_ms,
            }
        )
    )


def _cfg(tmp_path) -> PlaudOfficialConfig:
    return PlaudOfficialConfig(
        api_base=API, refresh_url=REFRESH_URL, tokens_path=tmp_path / "tokens.json"
    )


# --------------------------------------------------------------------------- #
# OAuth token store
# --------------------------------------------------------------------------- #


def test_missing_tokens_file_raises_helpfully(tmp_path):
    store = OfficialTokenStore(tmp_path / "nope.json", REFRESH_URL)
    with pytest.raises(OAuthError, match="auth login"):
        store.get_access_token()
    assert store.status()["ok"] is False


def test_valid_token_used_without_refresh(tmp_path):
    p = tmp_path / "tokens.json"
    _write_tokens(p, access="tok-live")
    store = OfficialTokenStore(p, REFRESH_URL)
    assert store.get_access_token() == "tok-live"  # no HTTP mock -> would blow up
    assert store.status() == {"ok": True, "detail": "valid"}


def test_native_pkce_request_uses_official_public_client():
    request = create_authorization_request(
        "https://web.plaud.ai/platform/oauth",
        "client-public",
        "http://localhost:8199/auth/callback",
    )
    params = parse_qs(urlparse(request.url).query)
    assert params["client_id"] == ["client-public"]
    assert params["redirect_uri"] == ["http://localhost:8199/auth/callback"]
    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == [request.state]
    assert params["code_challenge"][0]
    assert request.code_verifier not in request.url


@respx.mock
def test_native_code_exchange_persists_private_cli_compatible_tokens(tmp_path):
    token_url = f"{API}/oauth/third-party/access-token"
    route = respx.post(token_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "native-access",
                "refresh_token": "native-refresh",
                "token_type": "Bearer",
                "expires_in": 86400,
            },
        )
    )
    path = tmp_path / "tokens.json"
    store = OfficialTokenStore(path, REFRESH_URL)
    tokens = exchange_authorization_code(
        token_url=token_url,
        client_id="client-public",
        redirect_uri="http://localhost:8199/auth/callback",
        code="auth-code",
        code_verifier="verifier",
        state="expected-state",
        store=store,
    )
    assert tokens["access_token"] == "native-access"
    request = route.calls[0].request
    assert request.headers["authorization"].startswith("Basic ")
    assert b"code_verifier=verifier" in request.content
    assert b"state=expected-state" in request.content
    assert json.loads(path.read_text())["refresh_token"] == "native-refresh"
    assert os.stat(path).st_mode & 0o777 == 0o600


@respx.mock
def test_native_code_exchange_requires_refresh_token(tmp_path):
    token_url = f"{API}/oauth/third-party/access-token"
    respx.post(token_url).mock(
        return_value=httpx.Response(200, json={"access_token": "short-lived"})
    )
    with pytest.raises(OAuthError, match="no refresh_token"):
        exchange_authorization_code(
            token_url=token_url,
            client_id="client-public",
            redirect_uri="http://localhost:8199/auth/callback",
            code="auth-code",
            code_verifier="verifier",
            state="expected-state",
            store=OfficialTokenStore(tmp_path / "tokens.json", REFRESH_URL),
        )
    assert not (tmp_path / "tokens.json").exists()


def test_loopback_callback_ignores_wrong_state_then_exchanges():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    ready = threading.Event()
    exchanged = []
    errors = []

    def run():
        try:
            run_loopback_callback(
                expected_state="right",
                exchange_code=exchanged.append,
                port=port,
                timeout=3,
                on_listening=ready.set,
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert ready.wait(1)
    wrong = httpx.get(
        f"http://127.0.0.1:{port}/auth/callback?code=wrong&state=wrong"
    )
    assert wrong.status_code == 200
    assert exchanged == []
    success = httpx.get(
        f"http://127.0.0.1:{port}/auth/callback?code=good&state=right"
    )
    thread.join(2)
    assert success.status_code == 200
    assert exchanged == ["good"]
    assert errors == []
    assert not thread.is_alive()


def test_loopback_callback_surfaces_denial_timeout_and_busy_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        denied_port = probe.getsockname()[1]
    ready = threading.Event()
    errors = []

    def denied_run():
        try:
            run_loopback_callback(
                expected_state="right",
                exchange_code=lambda code: None,
                port=denied_port,
                timeout=2,
                on_listening=ready.set,
            )
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=denied_run)
    thread.start()
    assert ready.wait(1)
    response = httpx.get(
        f"http://127.0.0.1:{denied_port}/auth/callback"
        "?error=access_denied&error_description=Nope&state=right"
    )
    thread.join(2)
    assert response.status_code == 400
    assert len(errors) == 1 and "denied" in str(errors[0])

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        timeout_port = probe.getsockname()[1]
    with pytest.raises(OAuthError, match="timed out"):
        run_loopback_callback(
            expected_state="right",
            exchange_code=lambda code: None,
            port=timeout_port,
            timeout=0.02,
        )

    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        with pytest.raises(OAuthError, match="cannot bind OAuth callback"):
            run_loopback_callback(
                expected_state="right",
                exchange_code=lambda code: None,
                port=occupied.getsockname()[1],
                timeout=0.1,
            )


def test_native_login_shows_manual_url_when_browser_launch_raises(monkeypatch, tmp_path):
    shown = []
    monkeypatch.setattr(
        "localplaud.plaud.oauth.run_loopback_callback",
        lambda **kwargs: kwargs["on_listening"](),
    )

    def broken_browser(_url):
        raise RuntimeError("no desktop")

    path = native_login(
        _cfg(tmp_path), open_browser=broken_browser, show_manual_url=shown.append
    )
    assert path == tmp_path / "tokens.json"
    assert len(shown) == 1
    assert shown[0].startswith("https://web.plaud.ai/platform/oauth?")


@respx.mock
def test_expired_token_refreshes_and_persists(tmp_path):
    p = tmp_path / "tokens.json"
    _write_tokens(p, access="tok-old", refresh="ref-old", expires_in_ms=-1000)
    respx.post(REFRESH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok-new", "expires_in": 3600}
        )
    )
    store = OfficialTokenStore(p, REFRESH_URL)
    assert store.get_access_token() == "tok-new"
    saved = json.loads(p.read_text())
    assert saved["access_token"] == "tok-new"
    # The refresh response had no refresh_token -> the old one is kept.
    assert saved["refresh_token"] == "ref-old"
    assert saved["expires_at"] > time.time() * 1000


@respx.mock
def test_failed_refresh_raises_oauth_error(tmp_path):
    p = tmp_path / "tokens.json"
    _write_tokens(p, expires_in_ms=-1000)
    respx.post(REFRESH_URL).mock(return_value=httpx.Response(401, text="revoked"))
    store = OfficialTokenStore(p, REFRESH_URL)
    with pytest.raises(OAuthError, match="refresh failed"):
        store.get_access_token()


# --------------------------------------------------------------------------- #
# DTO normalization
# --------------------------------------------------------------------------- #


def test_parse_iso_ms_treats_naive_as_utc():
    assert _parse_iso_ms("1970-01-01T00:00:01") == 1000
    assert _parse_iso_ms(None) is None
    assert _parse_iso_ms("garbage") is None


def test_to_dto_maps_and_leaves_unknowns_unset():
    dto = _to_dto(
        {
            "id": "f1",
            "name": "My meeting",
            "created_at": "2026-07-09T10:50:42",
            "serial_number": "888",
            "start_at": "1970-01-01T00:00:01",
            "duration": "2000",
        }
    )
    assert dto.filename == "My meeting"
    assert dto.duration == 2000
    assert dto.start_time == 1000
    assert dto.end_time == 3000
    assert dto.model_fields_set == {
        "id", "filename", "duration", "start_time", "end_time", "serial_number"
    }


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


@respx.mock
def test_iter_files_pages_until_short_page(tmp_path):
    _write_tokens(tmp_path / "tokens.json")

    def pageful(request):
        page = int(request.url.params["page"])
        size = int(request.url.params["page_size"])
        count = size if page == 1 else 3  # short second page ends the walk
        items = [
            {"id": f"p{page}-{i}", "name": "n", "duration": "1000"} for i in range(count)
        ]
        return httpx.Response(200, json={"type": "files", "data": items, "page": page})

    respx.get(f"{API}/open/third-party/files/").mock(side_effect=pageful)
    with PlaudOfficialClient(_cfg(tmp_path)) as c:
        files = list(c.iter_files())
    assert len(files) == 103
    assert files[0].id == "p1-0" and files[-1].id == "p2-2"


@respx.mock
def test_401_forces_one_refresh_then_retries(tmp_path):
    p = tmp_path / "tokens.json"
    _write_tokens(p, access="tok-stale")  # not expired by clock, but revoked
    respx.post(REFRESH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok-fresh", "expires_in": 3600}
        )
    )

    def route(request):
        auth = request.headers["Authorization"]
        if auth == "Bearer tok-stale":
            return httpx.Response(401)
        return httpx.Response(200, json={"id": "u1", "email": "e@x"})

    respx.get(f"{API}/open/third-party/users/current").mock(side_effect=route)
    with PlaudOfficialClient(_cfg(tmp_path)) as c:
        me = c.check_auth()
    assert me["id"] == "u1"
    assert json.loads(p.read_text())["access_token"] == "tok-fresh"


@respx.mock
def test_429_retries_with_backoff_until_success(tmp_path, monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)  # no real waits
    _write_tokens(tmp_path / "tokens.json")
    responses = iter([httpx.Response(429), httpx.Response(429),
                      httpx.Response(200, json={"id": "u1"})])
    respx.get(f"{API}/open/third-party/users/current").mock(
        side_effect=lambda request: next(responses)
    )
    with PlaudOfficialClient(_cfg(tmp_path)) as c:
        assert c.check_auth()["id"] == "u1"


@respx.mock
def test_auth_error_when_refresh_cannot_save_the_day(tmp_path):
    p = tmp_path / "tokens.json"
    _write_tokens(p, access="tok-stale")
    respx.post(REFRESH_URL).mock(return_value=httpx.Response(400, text="nope"))
    respx.get(f"{API}/open/third-party/users/current").mock(
        return_value=httpx.Response(401)
    )
    with PlaudOfficialClient(_cfg(tmp_path)) as c, pytest.raises(PlaudAuthError):
        c.check_auth()


def _detail(fid: str, **extra) -> dict:
    return {
        "id": fid,
        "name": "07-09 A meeting",
        "created_at": "2026-07-09T10:50:18",
        "serial_number": "888",
        "start_at": "2026-07-09T07:38:57",
        "duration": "2489000",
        "presigned_url": None,
        "source_list": [],
        "note_list": [],
        **extra,
    }


@respx.mock
def test_download_audio_uses_presigned_url(tmp_path, monkeypatch):
    monkeypatch.setattr("localplaud.plaud.official._assert_safe_fetch_url", lambda u: None)
    _write_tokens(tmp_path / "tokens.json")
    fid = "abc"
    signed = f"https://bucket.s3-accelerate.amazonaws.com/audiofiles/{fid}.mp3?Signature=s"
    respx.get(f"{API}/open/third-party/files/{fid}").mock(
        return_value=httpx.Response(200, json=_detail(fid, presigned_url=signed))
    )
    respx.get(url__regex=r".*audiofiles/abc\.mp3.*").mock(
        return_value=httpx.Response(200, content=b"ID3fakeaudio")
    )
    from localplaud.plaud.models import PlaudFileDTO

    with PlaudOfficialClient(_cfg(tmp_path)) as c:
        dest = c.download_audio(PlaudFileDTO(id=fid), tmp_path / "out")
    assert dest.name == "audio.mp3"
    assert dest.read_bytes() == b"ID3fakeaudio"


@respx.mock
def test_cloud_artifacts_extracted_from_detail(tmp_path):
    _write_tokens(tmp_path / "tokens.json")
    fid = "withart"
    segments = [
        {"content": " hi ", "start_time": 1000, "end_time": 2500,
         "speaker": "Sky", "original_speaker": "Speaker 1", "embeddingKey": None},
        {"content": "there", "start_time": 2500, "end_time": 4000,
         "speaker": None, "original_speaker": "Speaker 2", "embeddingKey": None},
    ]
    detail = _detail(
        fid,
        source_list=[
            {"data_type": "outline", "data_content": "{}"},
            {"data_type": "transaction", "data_content": json.dumps(segments)},
        ],
        note_list=[
            {"data_type": "auto_sum_note", "data_title": "Summary",
             "data_content": "# Title\n\nbody"},
            {"data_type": "action_items", "data_content": "intro\n# Actions\n\n- One"},
            {"data_type": "empty", "data_content": ""},
        ],
    )
    respx.get(f"{API}/open/third-party/files/{fid}").mock(
        return_value=httpx.Response(200, json=detail)
    )
    with PlaudOfficialClient(_cfg(tmp_path)) as c:
        assert c.get_cloud_summary_md(fid) == "# Title\n\nbody"
        assert c.get_cloud_notes(fid) == [
            {"key": "auto_sum_note", "title": "Title", "markdown": "# Title\n\nbody"},
            {
                "key": "action_items",
                "title": "Actions",
                "markdown": "intro\n# Actions\n\n- One",
            },
        ]
        segs = c.get_cloud_transcript_segments(fid)
    assert segs == [
        {"text": "hi", "start": 1.0, "end": 2.5, "speaker": "Sky"},
        {"text": "there", "start": 2.5, "end": 4.0, "speaker": "Speaker 2"},
    ]
    # Detail was cached: all three reads cost one HTTP call.
    assert respx.calls.call_count == 1


# --------------------------------------------------------------------------- #
# Poller integration: unknown fields must not clobber or false-trigger
# --------------------------------------------------------------------------- #


def _reset_db(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)


def test_official_sync_keeps_enriched_fields_and_stays_quiet(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.config import get_settings
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import init_db, session_scope
    from localplaud.poller.poll import sync_file_list

    init_db()
    with session_scope() as s:
        s.add(
            PlaudFile(
                id="f1", status=FileStatus.done, audio_path="/a.mp3",
                file_md5="MD5", version=7, version_ms=7, is_trash=False,
                cloud_is_trans=True, filename="old name",
            )
        )

    class OfficialFake:
        def iter_files(self, include_trash=False):
            yield _to_dto({"id": "f1", "name": "new name", "duration": "1000"})

    new, changed = sync_file_list(OfficialFake(), get_settings())
    # No version info from the Open API -> "unknown", never "changed".
    assert (new, changed) == (0, 0)
    with session_scope() as s:
        row = s.get(PlaudFile, "f1")
        assert row.status == FileStatus.done  # untouched
        assert row.filename == "new name"
        # Existing locally retained fields survive the minimal official pass.
        assert (row.file_md5, row.version, row.cloud_is_trans) == ("MD5", 7, True)
