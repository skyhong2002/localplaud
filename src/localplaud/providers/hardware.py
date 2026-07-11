"""Truthful local hardware/runtime detection and ranked ASR recommendations."""

from __future__ import annotations

import importlib.util
import platform
import shutil
import subprocess
from pathlib import Path

from ..config import Settings, get_settings


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _memory_bytes(system: str) -> int | None:
    if system == "Darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
            return int(result.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _nvidia_gpus() -> list[dict]:
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return []
    try:
        result = subprocess.run(
            [
                binary,
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, _, memory = line.rpartition(",")
        try:
            memory_mb = int(memory.strip())
        except ValueError:
            memory_mb = None
        gpus.append({"name": name.strip() or line.strip(), "memory_mb": memory_mb})
    return gpus


def _ctranslate_cuda_devices() -> int:
    """Probe CTranslate2 itself; an NVIDIA driver alone is not enough."""
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except (ImportError, OSError, RuntimeError, ValueError):
        return 0


def detect_local_hardware(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    system = platform.system()
    machine = platform.machine().lower()
    memory = _memory_bytes(system)
    cuda_devices = _ctranslate_cuda_devices()
    return {
        "system": system,
        "machine": machine,
        "processor": platform.processor() or None,
        "memory_bytes": memory,
        "memory_gb": round(memory / 1024**3, 1) if memory else None,
        "apple_silicon": system == "Darwin" and machine in {"arm64", "aarch64"},
        "nvidia_gpus": _nvidia_gpus(),
        "runtimes": {
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "mlx_whisper": _module_available("mlx_whisper"),
            "faster_whisper": _module_available("faster_whisper"),
            "ctranslate2_cuda": cuda_devices > 0,
            "whispercpp": (
                shutil.which(settings.asr.whispercpp.binary) is not None
                and settings.asr.whispercpp.model_path.exists()
            ),
        },
    }


def hardware_recommendations(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    host = detect_local_hardware(settings)
    runtime = host["runtimes"]
    recommendations: list[dict] = []

    if host["apple_silicon"]:
        missing = [name for name in ("ffmpeg", "mlx_whisper") if not runtime[name]]
        recommendations.append(
            {
                "key": "apple-mlx-local",
                "name": "Apple Silicon · MLX Whisper",
                "rank": 10,
                "ready": not missing,
                "provider": "mlx-whisper",
                "model": settings.asr.mlx_whisper.model,
                "options": {},
                "hardware": "Apple Silicon Metal",
                "reason": (
                    "MLX runtime and ffmpeg detected"
                    if not missing
                    else f"Install: {', '.join(missing)}"
                ),
                "missing": missing,
            }
        )

    if host["nvidia_gpus"]:
        missing = [
            name for name in ("ffmpeg", "faster_whisper", "ctranslate2_cuda") if not runtime[name]
        ]
        gpu = host["nvidia_gpus"][0]
        recommendations.append(
            {
                "key": "nvidia-cuda-local",
                "name": "NVIDIA CUDA · faster-whisper",
                "rank": 10,
                "ready": not missing,
                "provider": "faster-whisper",
                "model": settings.asr.faster_whisper.model,
                "options": {"device": "cuda", "compute_type": "float16"},
                "hardware": f"{gpu['name']} · {gpu['memory_mb'] or '?'} MB",
                "reason": (
                    "NVIDIA driver, CTranslate2 CUDA, faster-whisper, and ffmpeg detected"
                    if not missing
                    else f"Install: {', '.join(missing)}"
                ),
                "missing": missing,
            }
        )

    cpu_missing = [name for name in ("ffmpeg", "faster_whisper") if not runtime[name]]
    recommendations.append(
        {
            "key": "cpu-faster-whisper-local",
            "name": "CPU · faster-whisper",
            "rank": 30,
            "ready": not cpu_missing,
            "provider": "faster-whisper",
            "model": settings.asr.faster_whisper.model,
            "options": {"device": "cpu", "compute_type": "int8"},
            "hardware": f"{host['machine']} · {host['memory_gb'] or '?'} GB RAM",
            "reason": (
                "Portable CPU fallback; slower than a verified accelerator"
                if not cpu_missing
                else f"Install: {', '.join(cpu_missing)}"
            ),
            "missing": cpu_missing,
        }
    )
    recommendations.sort(key=lambda item: (item["rank"], not item["ready"], item["key"]))
    return {"host": host, "recommendations": recommendations}
