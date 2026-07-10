"""Settings: env-var overrides, nested defaults, layered sources."""

import os

from localplaud.config import Settings


def _isolate(monkeypatch, tmp_path):
    """Remove ambient LOCALPLAUD_* env vars and any config.toml/.env in cwd,
    so tests see only what they set themselves."""
    for key in list(os.environ):
        if key.startswith("LOCALPLAUD_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)  # no config.toml or .env here


def test_env_vars_override_defaults(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCALPLAUD_ASR__PROVIDER", "deepgram")
    monkeypatch.setenv("LOCALPLAUD_PLAUD__API_BASE", "https://x")

    s = Settings()

    assert s.asr.provider == "deepgram"
    assert s.plaud.api_base == "https://x"

    # Untouched nested defaults still exist alongside the overrides.
    assert s.asr.language == "auto"
    assert s.asr.faster_whisper.model == "large-v3-turbo"
    assert s.asr.deepgram.model == "nova-2"
    assert s.poller.interval_seconds == 300
    assert s.store.database_url.startswith("sqlite:///")


def test_deeply_nested_env_override(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCALPLAUD_ASR__OPENAI__API_KEY", "sk-test")

    s = Settings()

    assert s.asr.openai.api_key == "sk-test"
    assert s.asr.openai.model == "whisper-1"  # sibling default intact


def test_defaults_without_env(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    s = Settings()

    assert s.asr.provider == "faster-whisper"
    assert s.plaud.auth_mode == "cookie"
    assert s.pipeline.transcribe is True
    assert s.diarize.provider == "pyannote"


def test_get_settings_reload(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    import localplaud.config as config

    monkeypatch.setenv("LOCALPLAUD_ASR__PROVIDER", "openai")
    s = config.get_settings(reload=True)
    assert s.asr.provider == "openai"
    # Singleton is returned as-is without reload.
    assert config.get_settings() is s

    # Leave a clean singleton for other tests (env is restored by monkeypatch
    # at teardown, but the cached object would not be).
    monkeypatch.delenv("LOCALPLAUD_ASR__PROVIDER")
    assert config.get_settings(reload=True).asr.provider == "faster-whisper"
