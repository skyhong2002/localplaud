"""OpenCode Go is invoked through a tool-free CLI agent without token reuse."""

from __future__ import annotations

import json
from subprocess import CompletedProcess, TimeoutExpired

import pytest

from localplaud.config import OpenCodeGoLlmConfig
from localplaud.llm.base import LLMError
from localplaud.llm.opencode_go import OpenCodeGoLLM


def test_opencode_go_uses_stdin_and_parses_text_events(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured.update(command=command, **kwargs)
        event = {"type": "text", "part": {"text": '{"segments":[]}'}}
        return CompletedProcess(command, 0, stdout=json.dumps(event) + "\n", stderr="")

    monkeypatch.setattr("localplaud.llm.opencode_go.shutil.which", lambda _name: "/bin/opencode")
    monkeypatch.setattr("localplaud.llm.opencode_go.subprocess.run", fake_run)
    provider = OpenCodeGoLLM(OpenCodeGoLlmConfig())

    assert provider.complete("private transcript", system="rules") == '{"segments":[]}'
    assert captured["input"] == "rules\n\nprivate transcript"
    assert "private transcript" not in captured["command"]
    assert captured["command"][-2:] == ["--title", "localplaud transcript polish"]
    assert "--agent" in captured["command"]
    assert captured["capture_output"] is True


def test_opencode_go_timeout_is_actionable(monkeypatch):
    monkeypatch.setattr("localplaud.llm.opencode_go.shutil.which", lambda _name: "/bin/opencode")
    monkeypatch.setattr(
        "localplaud.llm.opencode_go.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutExpired("opencode", 30)),
    )
    provider = OpenCodeGoLLM(OpenCodeGoLlmConfig(timeout_seconds=30))
    with pytest.raises(LLMError, match="timed out after 30s"):
        provider.complete("text")


def test_opencode_go_health_checks_credential_and_model(monkeypatch):
    responses = iter(
        [
            CompletedProcess([], 0, stdout="OpenCode Go api\n", stderr=""),
            CompletedProcess([], 0, stdout="opencode-go/qwen3.7-plus\n", stderr=""),
        ]
    )
    monkeypatch.setattr("localplaud.llm.opencode_go.shutil.which", lambda _name: "/bin/opencode")
    monkeypatch.setattr(
        "localplaud.llm.opencode_go.subprocess.run", lambda *_args, **_kwargs: next(responses)
    )
    ok, detail = OpenCodeGoLLM(OpenCodeGoLlmConfig()).health()
    assert ok is True
    assert "credential and model" in detail
