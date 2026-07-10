"""Config layering: TOML file in cwd is read; env vars beat TOML; TOML beats
built-in defaults; LOCALPLAUD_CONFIG points at an alternate file."""

from __future__ import annotations

import os

from localplaud.config import Settings


def _isolate(monkeypatch, tmp_path):
    """Remove ambient LOCALPLAUD_* env vars and run from an empty tmp cwd so
    tests see only the layers they set up themselves."""
    for key in list(os.environ):
        if key.startswith("LOCALPLAUD_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


def test_config_toml_in_cwd_is_read(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "config.toml").write_text(
        """\
[asr]
provider = "deepgram"

[poller]
interval_seconds = 60
"""
    )

    s = Settings()

    # TOML values beat built-in defaults...
    assert s.asr.provider == "deepgram"
    assert s.poller.interval_seconds == 60
    # ...and untouched sections keep their defaults.
    assert s.llm.provider == "ollama"
    assert s.plaud.auth_mode == "cookie"


def test_env_var_overrides_toml(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "config.toml").write_text(
        """\
[asr]
provider = "deepgram"

[poller]
interval_seconds = 60
"""
    )
    monkeypatch.setenv("LOCALPLAUD_ASR__PROVIDER", "openai")

    s = Settings()

    assert s.asr.provider == "openai"  # env wins over TOML
    assert s.poller.interval_seconds == 60  # non-overridden TOML value survives


def test_localplaud_config_env_selects_file(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    alt = tmp_path / "elsewhere" / "mine.toml"
    alt.parent.mkdir()
    alt.write_text('[asr]\nprovider = "whispercpp"\n')
    # A config.toml in cwd exists too — the explicit path must win over it.
    (tmp_path / "config.toml").write_text('[asr]\nprovider = "deepgram"\n')
    monkeypatch.setenv("LOCALPLAUD_CONFIG", str(alt))

    s = Settings()

    assert s.asr.provider == "whispercpp"


def test_missing_toml_falls_back_to_defaults(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)  # empty cwd: no config.toml at all

    s = Settings()

    assert s.asr.provider == "faster-whisper"
    assert s.poller.interval_seconds == 300
