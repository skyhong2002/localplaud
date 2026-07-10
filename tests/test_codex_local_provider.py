from types import SimpleNamespace

import pytest

from localplaud.config import CodexLocalConfig
from localplaud.llm.base import LLMUnavailable
from localplaud.llm.codex_local import CodexLocalProvider


def test_codex_local_is_disabled_by_default():
    with pytest.raises(LLMUnavailable, match="disabled"):
        CodexLocalProvider(CodexLocalConfig()).complete("private transcript")


def test_codex_local_uses_supported_ephemeral_read_only_cli_boundary(monkeypatch):
    seen = {}
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/codex")

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        output = command[command.index("--output-last-message") + 1]
        from pathlib import Path

        Path(output).write_text("grounded answer")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    provider = CodexLocalProvider(CodexLocalConfig(enabled=True, model="gpt-test"))
    assert provider.complete("transcript prompt", system="stay grounded") == "grounded answer"
    command = seen["command"]
    assert ["--ephemeral", "--ignore-user-config"] == command[4:6]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[-1] == "-"
    assert "transcript prompt" in seen["kwargs"]["input"]
    assert all("PLAUD" not in key for key in seen["kwargs"]["env"])
