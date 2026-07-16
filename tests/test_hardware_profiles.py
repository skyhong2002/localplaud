"""Local hardware recommendations are truthful and safe to install."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from localplaud.config import Settings
from localplaud.db.models import Base


def test_apple_recommendation_requires_real_runtime(monkeypatch):
    import localplaud.providers.hardware as hardware

    monkeypatch.setattr(hardware.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hardware.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(hardware.platform, "processor", lambda: "Apple M4")
    monkeypatch.setattr(hardware, "_memory_bytes", lambda _system: 16 * 1024**3)
    monkeypatch.setattr(hardware, "_nvidia_gpus", lambda: [])
    monkeypatch.setattr(hardware, "_ctranslate_cuda_devices", lambda: 0)
    monkeypatch.setattr(hardware, "_module_available", lambda name: name == "mlx_whisper")
    monkeypatch.setattr(
        hardware.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "ffmpeg" else None
    )

    result = hardware.hardware_recommendations(Settings())
    assert result["host"]["apple_silicon"] is True
    assert result["host"]["memory_gb"] == 16.0
    apple = result["recommendations"][0]
    assert apple["key"] == "apple-mlx-local" and apple["ready"] is True
    assert apple["provider"] == "mlx-whisper"
    assert "Metal" in apple["hardware"]

    monkeypatch.setattr(hardware, "_module_available", lambda _name: False)
    apple = hardware.hardware_recommendations(Settings())["recommendations"][0]
    assert apple["ready"] is False
    assert "mlx_whisper" in apple["missing"]


def test_nvidia_and_cpu_recommendations_declare_explicit_devices(monkeypatch):
    import localplaud.providers.hardware as hardware

    monkeypatch.setattr(hardware.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hardware.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(hardware.platform, "processor", lambda: "")
    monkeypatch.setattr(hardware, "_memory_bytes", lambda _system: 32 * 1024**3)
    monkeypatch.setattr(hardware, "_nvidia_gpus", lambda: [{"name": "RTX 5060", "memory_mb": 8192}])
    monkeypatch.setattr(hardware, "_ctranslate_cuda_devices", lambda: 1)
    monkeypatch.setattr(hardware, "_module_available", lambda name: name == "faster_whisper")
    monkeypatch.setattr(
        hardware.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "ffmpeg" else None
    )

    recommendations = hardware.hardware_recommendations(Settings())["recommendations"]
    nvidia = next(item for item in recommendations if item["key"] == "nvidia-cuda-local")
    cpu = next(item for item in recommendations if item["key"] == "cpu-faster-whisper-local")
    assert nvidia["ready"] is True
    assert nvidia["options"] == {"device": "cuda", "compute_type": "float16"}
    assert "RTX 5060" in nvidia["hardware"]
    assert cpu["options"] == {"device": "cpu", "compute_type": "int8"}


def _ready_apple():
    return {
        "host": {"system": "Darwin", "machine": "arm64"},
        "recommendations": [
            {
                "key": "apple-mlx-local",
                "name": "Apple Silicon · MLX Whisper",
                "ready": True,
                "provider": "mlx-whisper",
                "model": "mlx-community/whisper-large-v3-turbo",
                "options": {},
                "hardware": "Apple Silicon Metal",
                "reason": "MLX runtime and ffmpeg detected",
                "missing": [],
            }
        ],
    }


def test_install_recommendation_clones_policy_and_other_stages(monkeypatch, tmp_path):
    import localplaud.providers.hardware as hardware
    from localplaud.providers.service import (
        bootstrap_default_profile,
        install_hardware_recommendation,
        list_profiles,
    )

    monkeypatch.setattr(hardware, "hardware_recommendations", _ready_apple)
    engine = create_engine(f"sqlite:///{tmp_path / 'profiles.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        original = bootstrap_default_profile(session, Settings())
        session.commit()
        original_profile = next(
            item for item in list_profiles(session) if item["id"] == original.id
        )
        created = install_hardware_recommendation(session, "apple-mlx-local")
        session.commit()
        assert created["key"] == "recommended-apple-mlx-local"
        assert created["is_system_default"] is False
        assert created["policy"] == original_profile["policy"]
        assert created["stages"]["transcribe"]["connection"] == "asr:mlx-whisper"
        assert created["stages"]["align"]["connection"] == "asr:mlx-whisper"
        for stage in ("diarize", "summarize", "mind_map", "embed", "ask"):
            assert created["stages"][stage] == original_profile["stages"][stage]

        sync_calls: list[bool] = []
        monkeypatch.setattr(
            "localplaud.worker.knowledge_index.sync_knowledge_documents",
            lambda _session: sync_calls.append(True),
        )
        again = install_hardware_recommendation(session, "apple-mlx-local", make_default=True)
        session.commit()
        assert again["id"] == created["id"]
        assert again["is_system_default"] is True
        assert len([p for p in list_profiles(session) if p["key"] == created["key"]]) == 1
        assert sync_calls == [True]


def test_unready_recommendation_cannot_be_installed(monkeypatch, tmp_path):
    import localplaud.providers.hardware as hardware
    from localplaud.providers.service import (
        bootstrap_default_profile,
        install_hardware_recommendation,
    )

    unavailable = _ready_apple()
    unavailable["recommendations"][0] |= {
        "ready": False,
        "reason": "Install: mlx_whisper",
        "missing": ["mlx_whisper"],
    }
    monkeypatch.setattr(hardware, "hardware_recommendations", lambda: unavailable)
    engine = create_engine(f"sqlite:///{tmp_path / 'unready.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        bootstrap_default_profile(session, Settings())
        with pytest.raises(ValueError, match="mlx_whisper"):
            install_hardware_recommendation(session, "apple-mlx-local")


def test_recommendation_api_and_settings_install_profile(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.config as config
    import localplaud.db.session as db_session
    import localplaud.providers.hardware as hardware

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    monkeypatch.setattr(hardware, "hardware_recommendations", _ready_apple)
    config.get_settings(reload=True)
    from localplaud.api.app import app

    with TestClient(app) as client:
        detected = client.get("/api/providers/hardware-recommendations")
        assert detected.status_code == 200
        assert detected.json()["recommendations"][0]["ready"] is True
        page = client.get("/settings")
        assert page.status_code == 200
        assert "Recommended for this host" in page.text
        assert "Apple Silicon · MLX Whisper" in page.text
        created = client.post(
            "/api/providers/hardware-recommendations/apple-mlx-local/install",
            json={"make_default": True},
        )
        assert created.status_code == 201
        assert created.json()["is_system_default"] is True
        second = client.post(
            "/api/providers/hardware-recommendations/apple-mlx-local/install",
            json={"make_default": True},
        )
        assert second.status_code == 201
        assert second.json()["id"] == created.json()["id"]
