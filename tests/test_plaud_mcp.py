from __future__ import annotations

import pytest


def _client_without_process(monkeypatch):
    from localplaud.config import PlaudMcpConfig
    from localplaud.plaud.mcp import PlaudMcpClient

    monkeypatch.setattr(PlaudMcpClient, "__init__", lambda self, cfg: setattr(self, "cfg", cfg))
    return PlaudMcpClient(PlaudMcpConfig())


def test_mcp_provider_builds_from_config(monkeypatch):
    import localplaud.plaud.mcp as module
    from localplaud.config import PlaudConfig
    from localplaud.plaud import make_plaud_client

    monkeypatch.setattr(
        module.PlaudMcpClient, "__init__", lambda self, cfg: setattr(self, "cfg", cfg)
    )
    client = make_plaud_client(PlaudConfig(provider="mcp"))
    assert isinstance(client, module.PlaudMcpClient)
    assert client.cfg.command == "npx"


def test_mcp_listing_normalizes_shared_dto(monkeypatch):
    client = _client_without_process(monkeypatch)
    monkeypatch.setattr(
        client,
        "_call_tool",
        lambda name, args=None: {
            "data": [
                {
                    "id": "m1",
                    "name": "Meeting",
                    "start_at": "2026-07-12T01:02:03",
                    "duration": 65000,
                    "serial_number": "device",
                }
            ]
        },
    )
    files = list(client.iter_files())
    assert len(files) == 1
    assert files[0].id == "m1"
    assert files[0].filename == "Meeting"
    assert files[0].duration == 65000


def test_mcp_cloud_artifacts_stay_explicit(monkeypatch):
    client = _client_without_process(monkeypatch)
    responses = {
        "get_note": {"markdown": "# Imported Plaud note"},
        "get_transcript": {"segments": [{"text": "hello", "start": 0, "end": 1}]},
    }
    monkeypatch.setattr(client, "_call_tool", lambda name, args=None: responses[name])
    assert client.get_cloud_summary_md("m1") == "# Imported Plaud note"
    assert client.get_cloud_transcript_segments("m1") == [{"text": "hello", "start": 0, "end": 1}]


def test_apse1_provider_emits_deprecation_warning(monkeypatch):
    import localplaud.plaud.client as module
    from localplaud.config import PlaudConfig
    from localplaud.plaud import make_plaud_client

    monkeypatch.setattr(module.PlaudClient, "__init__", lambda self, cfg: None)
    with pytest.deprecated_call(match="apse1"):
        make_plaud_client(PlaudConfig(provider="apse1"))


def test_mcp_auth_status_does_not_read_or_expose_tokens(tmp_path):
    from localplaud.config import PlaudMcpConfig
    from localplaud.plaud.mcp import PlaudMcpClient

    path = tmp_path / "tokens-mcp.json"
    cfg = PlaudMcpConfig(tokens_path=path)
    assert PlaudMcpClient.auth_status(cfg)["ok"] is False
    path.write_text('{"access_token":"do-not-expose"}', encoding="utf-8")
    status = PlaudMcpClient.auth_status(cfg)
    assert status["ok"] is True
    assert "do-not-expose" not in str(status)
