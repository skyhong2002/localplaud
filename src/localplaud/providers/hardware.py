"""Conservative local runtime detection for profile recommendations."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class HardwareReport:
    system: str
    machine: str
    apple_silicon: bool
    nvidia_runtime: bool
    mlx_whisper_installed: bool
    faster_whisper_installed: bool
    whispercpp_installed: bool
    pyannote_installed: bool
    memory_gb: float | None
    nvidia_gpus: tuple[dict, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError):
        return False


def detect_hardware() -> HardwareReport:
    system = platform.system().lower()
    machine = platform.machine().lower()
    # nvidia-smi only proves a visible driver/runtime, not that CUDA ASR works.
    nvidia_runtime = shutil.which("nvidia-smi") is not None
    memory_bytes = None
    try:
        if system == "darwin":
            memory_bytes = int(subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], text=True, timeout=3
            ).strip())
        elif hasattr(os, "sysconf"):
            memory_bytes = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    nvidia_gpus = []
    if nvidia_runtime:
        try:
            output = subprocess.check_output([
                "nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits",
            ], text=True, timeout=5)
            for line in output.splitlines():
                name, memory = line.rsplit(",", 1)
                nvidia_gpus.append({"name": name.strip(), "memory_mb": int(memory.strip())})
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return HardwareReport(
        system=system,
        machine=machine,
        apple_silicon=system == "darwin" and machine in {"arm64", "aarch64"},
        nvidia_runtime=nvidia_runtime,
        mlx_whisper_installed=_installed("mlx_whisper"),
        faster_whisper_installed=_installed("faster_whisper"),
        whispercpp_installed=shutil.which("whisper-cli") is not None,
        pyannote_installed=_installed("pyannote.audio"),
        memory_gb=round(memory_bytes / (1024 ** 3), 1) if memory_bytes else None,
        nvidia_gpus=tuple(nvidia_gpus),
    )


def recommendations(report: HardwareReport | None = None) -> list[dict]:
    report = report or detect_hardware()
    choices = []
    if report.apple_silicon:
        enough_memory = report.memory_gb is None or report.memory_gb >= 8
        choices.append({"profile": "apple-local",
                        "recommended": report.mlx_whisper_installed and enough_memory,
                        "reason": "Apple Silicon detected; MLX runtime is " +
                        ("installed" if report.mlx_whisper_installed else "not installed")})
    if report.nvidia_runtime:
        enough_vram = not report.nvidia_gpus or max(
            item["memory_mb"] for item in report.nvidia_gpus
        ) >= 6000
        choices.append({"profile": "nvidia-local",
                        "recommended": report.faster_whisper_installed and enough_vram,
                        "reason": "NVIDIA runtime detected; CUDA ASR still requires a health test"})
    choices.append({"profile": "cpu-local", "recommended": not choices,
                    "reason": "Portable local profile; benchmark speed and memory on this host"})
    return choices
