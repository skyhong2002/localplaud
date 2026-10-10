"""Per-workspace Plaud account connections.

Each workspace connects its own Plaud account. The original workspace keeps the
configured transport and token cache (shared with the official CLI or MCP);
every other workspace uses the official read-only Open API with its own ``0600``
token file beside the local library.

The official Open API client is registered for a loopback redirect only, so the
Web flow is: localplaud creates a PKCE request, the user authorizes at Plaud,
their browser is sent to ``http://localhost:8199/auth/callback?...`` (which may
not load), and they paste that address back. localplaud verifies the state and
exchanges the code itself. Nothing beyond the authorization code is accepted
from the pasted address.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import PlaudConfig, Settings
from ..db.models import KeyValue, Workspace
from ..db.tenancy import (
    DEFAULT_WORKSPACE_ID,
    current_workspace_id,
    local_file_id,
    plaud_file_id,
    system_scope,
    workspace_key,
)
from .oauth import (
    OfficialTokenStore,
    create_authorization_request,
    exchange_authorization_code,
)

_PENDING_KEY = "plaud_oauth_pending_v1"
_ACCOUNT_KEY = "plaud_account_v1"
_PENDING_TTL_SECONDS = 15 * 60


class PlaudConnectionError(ValueError):
    """User-correctable connection failure; the message is safe to display."""


def tokens_path(settings: Settings, workspace_id: int | None = None) -> Path:
    workspace_id = workspace_id or current_workspace_id() or DEFAULT_WORKSPACE_ID
    if workspace_id == DEFAULT_WORKSPACE_ID:
        return settings.plaud.official.tokens_path.expanduser()
    root = Path(settings.poller.download_dir).expanduser().resolve().parent
    return root / "plaud-connections" / f"workspace-{workspace_id}.json"


def uses_web_connection(settings: Settings, workspace_id: int | None = None) -> bool:
    """Whether the workspace connects through the Web flow (official Open API)."""
    workspace_id = workspace_id or current_workspace_id() or DEFAULT_WORKSPACE_ID
    return workspace_id != DEFAULT_WORKSPACE_ID or settings.plaud.provider == "official"


def workspace_plaud_config(cfg: PlaudConfig, settings: Settings) -> PlaudConfig:
    """The Plaud configuration for the active workspace.

    The host's MCP session belongs to workspace 1, so every other workspace
    uses the official Open API with its own tokens.
    """
    workspace_id = current_workspace_id() or DEFAULT_WORKSPACE_ID
    if workspace_id == DEFAULT_WORKSPACE_ID:
        return cfg
    scoped = cfg.model_copy(deep=True)
    scoped.provider = "official"
    scoped.official.tokens_path = tokens_path(settings, workspace_id)
    return scoped


def _store(settings: Settings, workspace_id: int | None = None) -> OfficialTokenStore:
    official = settings.plaud.official
    return OfficialTokenStore(
        tokens_path(settings, workspace_id), official.refresh_url, official.request_timeout_seconds
    )


def connection_status(session: Session, settings: Settings) -> dict:
    workspace_id = current_workspace_id() or DEFAULT_WORKSPACE_ID
    web = uses_web_connection(settings)
    if web:
        status = _store(settings).status()
        if workspace_id != DEFAULT_WORKSPACE_ID:
            # Members never see host paths or token internals.
            status["detail"] = "已連結" if status["ok"] else "尚未連結"
    else:
        from .mcp import PlaudMcpClient

        status = PlaudMcpClient.auth_status(settings.plaud.mcp)
    account = session.get(KeyValue, workspace_key(_ACCOUNT_KEY))
    pending = session.get(KeyValue, workspace_key(_PENDING_KEY))
    return status | {
        "web_connect": web,
        "provider": "official" if web else settings.plaud.provider,
        "pending": bool(pending and (pending.value or {}).get("expires_at", 0) > time.time()),
        "connected_at": (account.value or {}).get("connected_at") if account else None,
    }


def start_connection(session: Session, settings: Settings) -> str:
    """Create a single-use PKCE request and return Plaud's authorization URL."""
    if not uses_web_connection(settings):
        raise PlaudConnectionError("這個工作區的 Plaud 連線由主機上的 Plaud MCP 設定管理。")
    official = settings.plaud.official
    request = create_authorization_request(
        official.authorization_url, official.client_id, official.redirect_uri
    )
    key = workspace_key(_PENDING_KEY)
    row = session.get(KeyValue, key)
    value = {
        "state": request.state,
        "verifier": request.code_verifier,
        "expires_at": time.time() + _PENDING_TTL_SECONDS,
    }
    if row is None:
        session.add(KeyValue(key=key, value=value))
    else:
        row.value = value
    session.flush()
    return request.url


def _plaud_account_id(profile: dict) -> str | None:
    data = profile.get("data") if isinstance(profile.get("data"), dict) else profile
    for field in ("id", "user_id", "uid", "email"):
        value = data.get(field)
        if value:
            return hashlib.sha256(str(value).strip().lower().encode()).hexdigest()
    return None


def finish_connection(session: Session, settings: Settings, pasted: str) -> dict:
    """Exchange the code in a pasted redirect address for this workspace's tokens."""
    key = workspace_key(_PENDING_KEY)
    pending = session.get(KeyValue, key)
    value = dict(pending.value or {}) if pending else {}
    if not value or value.get("expires_at", 0) < time.time():
        raise PlaudConnectionError("連線要求已過期，請重新按「連結 Plaud 帳號」。")
    official = settings.plaud.official
    parsed = urlparse(pasted.strip())
    expected = urlparse(official.redirect_uri)
    params = parse_qs(parsed.query)
    try:
        address = (parsed.hostname, parsed.port, parsed.path)
    except ValueError:
        address = None
    if address != (expected.hostname, expected.port, expected.path):
        raise PlaudConnectionError(
            "請貼上授權後瀏覽器網址列的完整網址（以 http://localhost 開頭）。"
        )
    if params.get("error"):
        raise PlaudConnectionError("Plaud 未授權這次連線，請重新嘗試。")
    state = (params.get("state") or [""])[0]
    code = (params.get("code") or [""])[0]
    if not code or state != value.get("state"):
        raise PlaudConnectionError("這個網址不屬於目前的連線要求，請重新按「連結 Plaud 帳號」。")
    # The request is single use, whatever happens next.
    session.delete(pending)
    session.flush()

    staging = _store(settings).tokens_path.with_suffix(".pending.json")
    staging_store = OfficialTokenStore(
        staging, official.refresh_url, official.request_timeout_seconds
    )
    try:
        exchange_authorization_code(
            token_url=official.token_url,
            client_id=official.client_id,
            redirect_uri=official.redirect_uri,
            code=code,
            code_verifier=value["verifier"],
            state=state,
            store=staging_store,
            timeout=official.request_timeout_seconds,
        )
        from .official import PlaudOfficialClient

        verify = official.model_copy(update={"tokens_path": staging})
        with PlaudOfficialClient(verify) as client:
            account_id = _plaud_account_id(client.check_auth() or {})
        if account_id and _account_used_elsewhere(session, account_id):
            raise PlaudConnectionError("這個 Plaud 帳號已連結到另一個工作區。")
        staging.chmod(0o600)
        staging.replace(_store(settings).tokens_path)
    except PlaudConnectionError:
        raise
    except Exception as exc:  # noqa: BLE001 - OAuth, network or Plaud API failure
        raise PlaudConnectionError("Plaud 授權失敗，請重新按「連結 Plaud 帳號」再試一次。") from exc
    finally:
        staging.unlink(missing_ok=True)
    account_key = workspace_key(_ACCOUNT_KEY)
    record = {
        "account": account_id,
        "connected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    row = session.get(KeyValue, account_key)
    if row is None:
        session.add(KeyValue(key=account_key, value=record))
    else:
        row.value = record
    session.flush()
    return connection_status(session, settings)


def _account_used_elsewhere(session: Session, account_id: str) -> bool:
    mine = workspace_key(_ACCOUNT_KEY)
    rows = session.scalars(
        select(KeyValue).where(KeyValue.key.like(f"%{_ACCOUNT_KEY}"), KeyValue.key != mine)
    )
    return any((row.value or {}).get("account") == account_id for row in rows)


def disconnect(session: Session, settings: Settings) -> None:
    """Forget this workspace's Plaud tokens. Recordings already copied stay."""
    if (current_workspace_id() or DEFAULT_WORKSPACE_ID) == DEFAULT_WORKSPACE_ID:
        raise PlaudConnectionError("原始工作區的 Plaud 連線由主機設定管理。")
    tokens_path(settings).unlink(missing_ok=True)
    for key in (_ACCOUNT_KEY, _PENDING_KEY):
        row = session.get(KeyValue, workspace_key(key))
        if row is not None:
            session.delete(row)
    session.flush()


def connected_workspace_ids(session: Session, settings: Settings) -> list[int]:
    """Workspaces whose Plaud account can be polled right now."""
    with system_scope():
        workspace_ids = list(session.scalars(select(Workspace.id).order_by(Workspace.id)))
    connected = []
    for workspace_id in workspace_ids:
        if workspace_id == DEFAULT_WORKSPACE_ID:
            connected.append(workspace_id)
        elif tokens_path(settings, workspace_id).exists():
            connected.append(workspace_id)
    return connected


class WorkspacePlaudClient:
    """A Plaud client whose recording ids are local to one workspace.

    Plaud ids are prefixed on the way in (listing) and unprefixed on the way
    out (detail, download, artifacts), so the rest of localplaud only ever sees
    workspace-local ids and two accounts can never collide.
    """

    def __init__(self, client, workspace_id: int):
        self._client = client
        self.workspace_id = workspace_id

    def __enter__(self) -> WorkspacePlaudClient:
        self._client.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self._client.__exit__(*exc)

    def close(self) -> None:
        self._client.close()

    def _local(self, dto):
        return dto.model_copy(update={"id": local_file_id(self.workspace_id, dto.id)})

    def _remote(self, file_id: str) -> str:
        return plaud_file_id(self.workspace_id, file_id)

    def check_auth(self) -> dict:
        return self._client.check_auth()

    def iter_files(self, *args, **kwargs):
        for dto in self._client.iter_files(*args, **kwargs):
            yield self._local(dto)

    def get_detail(self, file_id: str) -> dict:
        return self._client.get_detail(self._remote(file_id))

    def download_audio(self, file, dest_dir):
        return self._client.download_audio(
            file.model_copy(update={"id": self._remote(file.id)}), dest_dir
        )

    def get_cloud_summary_md(self, file_id: str, *args, **kwargs):
        return self._client.get_cloud_summary_md(self._remote(file_id), *args, **kwargs)

    def get_cloud_notes(self, file_id: str, *args, **kwargs):
        return self._client.get_cloud_notes(self._remote(file_id), *args, **kwargs)

    def get_cloud_transcript_segments(self, file_id: str, *args, **kwargs):
        return self._client.get_cloud_transcript_segments(self._remote(file_id), *args, **kwargs)

    def __getattr__(self, name: str):
        # Anything not translated above must not take a recording id.
        raise AttributeError(f"{name} is not available on a workspace Plaud client")
