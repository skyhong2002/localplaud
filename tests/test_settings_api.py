from __future__ import annotations

import threading
import time
import tomllib


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.config as config_module
    import localplaud.db.session as db_session
    from localplaud.api.app import app

    config = tmp_path / "config.toml"
    config.write_text(
        """# keep this operator comment
[store]
database_url = "sqlite:///settings-test.db"

[api]
host = "127.0.0.1"

[asr]
provider = "mlx-whisper"
language = "auto"

  [asr.mlx_whisper]
  model = "old-asr"

  [asr.openai]
  model = "old-openai-asr"
  headers = { X-Test = "preserve-me" }

[llm]
provider = "ollama"

  [llm.ollama]
  host = "http://localhost:11434"
  model = "old-llm"

[embeddings]
provider = "ollama"

  [embeddings.ollama]
  host = "http://localhost:11434"
  model = "old-embed"

[diarize]
provider = "pyannote"

[align]
provider = "whisper-timestamps"

[pipeline]
convert = true
transcribe = true
align = true
diarize = true
correct = true
summarize = true
mind_map = true
index = true
concurrency = 7
summary_template = "default"

[poller]
interval_seconds = 300
include_trash = true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALPLAUD_CONFIG", str(config))
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'settings.db'}")
    config_module._settings = None
    db_session._engine = None
    db_session._Session = None
    db_session.init_db()
    return TestClient(app), config


def _payload():
    return {
        "asr": {"provider": "openai", "model": 'gpt-4o-mini-transcribe "safe"', "language": "zh"},
        "llm": {"provider": "ollama", "model": "qwen3.5:9b"},
        "embeddings": {"provider": "ollama", "model": "bge-m3"},
        "diarize": {"provider": "pyannote"},
        "align": {"provider": "whisper-timestamps"},
        "pipeline": {
            "stages": {
                "convert": True,
                "transcribe": True,
                "align": True,
                "diarize": True,
                "correct": True,
                "summarize": True,
                "mind_map": True,
                "index": False,
            },
            "summary_template": "meeting",
        },
        "poller": {"interval_seconds": 120},
    }


def test_settings_save_is_valid_atomic_and_preserves_unrelated_config(monkeypatch, tmp_path):
    client, path = _client(monkeypatch, tmp_path)
    response = client.post("/api/settings", json=_payload())
    assert response.status_code == 200, response.text

    text = path.read_text(encoding="utf-8")
    saved = tomllib.loads(text)
    assert "# keep this operator comment" in text
    assert saved["asr"]["provider"] == "openai"
    assert saved["asr"]["openai"]["model"] == 'gpt-4o-mini-transcribe "safe"'
    assert saved["asr"]["openai"]["headers"] == {"X-Test": "preserve-me"}
    assert saved["pipeline"]["concurrency"] == 7
    assert saved["pipeline"]["index"] is False
    assert saved["poller"]["include_trash"] is True
    assert saved["poller"]["interval_seconds"] == 120
    assert not list(tmp_path.glob(".config.toml.*.tmp"))

    from localplaud.db.models import ExecutionProfile, StageName
    from localplaud.db.session import session_scope
    from localplaud.providers.resolver import resolve_profile

    with session_scope() as session:
        profile = session.query(ExecutionProfile).filter_by(is_system_default=True).one()
        route = resolve_profile(StageName.transcribe, system_default=profile)["route"]
        assert route["provider"] == "openai"
        assert route["model"] == 'gpt-4o-mini-transcribe "safe"'


def test_settings_rejects_invalid_values_without_touching_file(monkeypatch, tmp_path):
    client, path = _client(monkeypatch, tmp_path)
    before = path.read_bytes()
    payload = _payload()
    payload["asr"]["provider"] = "made-up-cloud"
    payload["poller"]["interval_seconds"] = 1

    response = client.post("/api/settings", json=payload)
    assert response.status_code == 422
    assert path.read_bytes() == before


def test_settings_preserves_full_whispercpp_model_path(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    payload = _payload()
    payload["asr"] = {
        "provider": "whispercpp",
        "model": "models/subdir/ggml-large-v3-turbo.bin",
        "language": "auto",
    }
    assert client.post("/api/settings", json=payload).status_code == 200
    page = client.get("/settings")
    assert page.status_code == 200
    assert "models/subdir/ggml-large-v3-turbo.bin" in page.text


def test_settings_transaction_lock_serializes_concurrent_writers(tmp_path):
    from localplaud.api.app import _settings_transaction_lock

    path = tmp_path / "config.toml"
    path.write_text("[pipeline]\n", encoding="utf-8")
    gate = threading.Barrier(3)
    state = {"active": 0, "max_active": 0}

    def writer():
        gate.wait()
        with _settings_transaction_lock(path):
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.03)
            state["active"] -= 1

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert state["max_active"] == 1
