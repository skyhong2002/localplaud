"""Each workspace connects and polls its own Plaud account."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from localplaud.db.models import PlaudFile, Workspace
from localplaud.db.tenancy import local_file_id, system_scope, workspace_scope
from localplaud.plaud.models import PlaudFileDTO


@pytest.fixture
def settings(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.session import init_db

    config = tmp_path / "empty.toml"
    config.write_text("")
    monkeypatch.setenv("LOCALPLAUD_CONFIG", str(config))
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "data" / "audio"))
    monkeypatch.setenv(
        "LOCALPLAUD_PLAUD__OFFICIAL__TOKENS_PATH", str(tmp_path / "owner-tokens.json")
    )
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    settings = get_settings(reload=True)
    init_db()
    with system_scope(), db_session.session_scope() as session:
        session.add_all([Workspace(id=2, name="two"), Workspace(id=3, name="three")])
    yield settings
    db_session.get_engine().dispose()


@pytest.fixture
def plaud_account(monkeypatch):
    """Fake Plaud: token exchange writes tokens; the profile names an account."""
    from localplaud.plaud import connections, official

    account = {"id": "plaud-user-a"}

    def exchange(**kwargs):
        kwargs["store"]._save(
            {"access_token": "a", "refresh_token": "r", "token_type": "Bearer", "expires_at": None}
        )

    monkeypatch.setattr(connections, "exchange_authorization_code", exchange)
    monkeypatch.setattr(official.PlaudOfficialClient, "check_auth", lambda self: account)
    return account


def _connect(session, settings, workspace_id):
    from localplaud.plaud.connections import finish_connection, start_connection

    with workspace_scope(workspace_id):
        url = start_connection(session, settings)
        state = parse_qs(urlsplit(url).query)["state"][0]
        pasted = f"http://localhost:8199/auth/callback?code=abc&state={state}"
        return finish_connection(session, settings, pasted)


def test_web_connection_is_per_workspace_and_single_use(settings, plaud_account):
    from localplaud.db.session import session_scope
    from localplaud.plaud.connections import (
        PlaudConnectionError,
        connected_workspace_ids,
        finish_connection,
        start_connection,
        tokens_path,
    )

    with session_scope() as session:
        assert connected_workspace_ids(session, settings) == [1]
        status = _connect(session, settings, 2)
        assert status["ok"] and not status["pending"]
        path = tokens_path(settings, 2)
        assert path.parent == settings.poller.download_dir.resolve().parent / "plaud-connections"
        assert path.stat().st_mode & 0o777 == 0o600
        assert not settings.plaud.official.tokens_path.exists()
        assert connected_workspace_ids(session, settings) == [1, 2]

        # The same Plaud account cannot back a second workspace.
        with pytest.raises(PlaudConnectionError, match="另一個工作區"):
            _connect(session, settings, 3)
        assert not tokens_path(settings, 3).exists()

        with workspace_scope(3):
            url = start_connection(session, settings)
            state = parse_qs(urlsplit(url).query)["state"][0]
            for pasted in (
                f"https://evil.example/auth/callback?code=x&state={state}",
                "http://localhost:8199/auth/callback?code=x&state=wrong",
            ):
                with pytest.raises(PlaudConnectionError):
                    finish_connection(session, settings, pasted)


def test_disconnect_forgets_only_that_workspace(settings, plaud_account):
    from localplaud.db.session import session_scope
    from localplaud.plaud.connections import PlaudConnectionError, disconnect, tokens_path

    with session_scope() as session:
        _connect(session, settings, 2)
        with workspace_scope(2):
            disconnect(session, settings)
        assert not tokens_path(settings, 2).exists()
        with workspace_scope(1), pytest.raises(PlaudConnectionError):
            disconnect(session, settings)
        plaud_account["id"] = "plaud-user-a"
        _connect(session, settings, 3)


def test_workspace_client_translates_ids_both_ways():
    from localplaud.plaud.connections import WorkspacePlaudClient

    calls = []

    class Remote:
        def iter_files(self, **kwargs):
            yield PlaudFileDTO(id="abc", filename="Recording")

        def get_detail(self, file_id):
            calls.append(file_id)
            return {}

        def download_audio(self, file, dest_dir):
            calls.append(file.id)
            return dest_dir

    client = WorkspacePlaudClient(Remote(), 5)
    [dto] = list(client.iter_files(include_trash=False))
    assert dto.id == "w5-abc"
    client.get_detail(dto.id)
    client.download_audio(dto, "/tmp")
    assert calls == ["abc", "abc"]
    with pytest.raises(Exception, match="not in workspace 5"):
        client.get_detail("w6-abc")
    with pytest.raises(AttributeError):
        _ = client.anything_else


def test_poll_syncs_every_connected_workspace_into_its_own_library(
    settings, plaud_account, monkeypatch
):
    from localplaud.db.session import session_scope
    from localplaud.plaud.connections import WorkspacePlaudClient
    from localplaud.poller import poll

    with session_scope() as session:
        _connect(session, settings, 2)

    listings = {1: ["owner-rec"], 2: ["same-id", "member-rec"]}

    class Remote:
        def __init__(self, workspace_id):
            self.workspace_id = workspace_id

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def iter_files(self, **kwargs):
            for file_id in listings[self.workspace_id]:
                yield PlaudFileDTO(id=file_id, filename=f"{file_id}.mp3")

    def factory(_config):
        from localplaud.db.tenancy import current_workspace_id

        workspace_id = current_workspace_id()
        remote = Remote(workspace_id)
        return remote if workspace_id == 1 else WorkspacePlaudClient(remote, workspace_id)

    monkeypatch.setattr(poll, "make_plaud_client", factory)
    listings[1].append("same-id")
    result = poll.poll_once(settings)
    assert result["new"] == 4
    with system_scope(), session_scope() as session:
        rows = dict(session.execute(select(PlaudFile.id, PlaudFile.workspace_id)).all())
    assert rows == {
        "owner-rec": 1,
        "same-id": 1,
        local_file_id(2, "same-id"): 2,
        local_file_id(2, "member-rec"): 2,
    }


def test_one_failing_account_does_not_stop_the_others(settings, plaud_account, monkeypatch):
    from localplaud.db.session import session_scope
    from localplaud.poller import poll

    with session_scope() as session:
        _connect(session, settings, 2)
    seen = []

    def sync(_client, _settings):
        from localplaud.db.tenancy import current_workspace_id

        seen.append(current_workspace_id())
        if current_workspace_id() == 2:
            raise RuntimeError("expired")
        return (0, 0)

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(poll, "make_plaud_client", lambda _config: Client())
    monkeypatch.setattr(poll, "sync_file_list", sync)
    poll.poll_once(settings)
    assert seen == [1, 2]


def test_members_connect_through_open_api_when_the_host_uses_mcp(
    settings, plaud_account, monkeypatch
):
    from localplaud.db.session import session_scope
    from localplaud.plaud import make_plaud_client
    from localplaud.plaud.connections import (
        PlaudConnectionError,
        WorkspacePlaudClient,
        connection_status,
        start_connection,
    )

    monkeypatch.setattr(settings.plaud, "provider", "mcp")
    with session_scope() as session:
        with workspace_scope(1), pytest.raises(PlaudConnectionError):
            start_connection(session, settings)
        status = _connect(session, settings, 2)
        assert status["ok"] and status["provider"] == "official"
        assert status["detail"] == "已連結" and "/" not in status["detail"]
        with workspace_scope(2):
            assert isinstance(make_plaud_client(settings.plaud), WorkspacePlaudClient)
        with workspace_scope(3):
            assert connection_status(session, settings)["detail"] == "尚未連結"


def test_connection_failures_are_user_correctable(settings, plaud_account, monkeypatch):
    from localplaud.db.session import session_scope
    from localplaud.plaud import connections

    def unreachable(**kwargs):
        raise OSError("network down")

    monkeypatch.setattr(connections, "exchange_authorization_code", unreachable)
    with session_scope() as session, workspace_scope(2):
        url = connections.start_connection(session, settings)
        state = parse_qs(urlsplit(url).query)["state"][0]
        with pytest.raises(connections.PlaudConnectionError):
            connections.finish_connection(
                session, settings, f"http://localhost:abc/auth/callback?code=x&state={state}"
            )
        with pytest.raises(connections.PlaudConnectionError, match="再試一次"):
            connections.finish_connection(
                session, settings, f"http://localhost:8199/auth/callback?code=x&state={state}"
            )
        assert not connections.tokens_path(settings, 2).exists()
