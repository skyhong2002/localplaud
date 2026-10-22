"""Side-effect-free capability health checks for durable provider connections."""

from __future__ import annotations

import shutil

from .hardware import detect_hardware


def check_provider(provider_type: str, secret_ref: str | None = None,
                   base_url: str | None = None) -> tuple[str, dict]:
    hardware = detect_hardware()
    if provider_type == "ffmpeg":
        ok = shutil.which("ffmpeg") is not None
        return ("healthy" if ok else "unavailable", {"binary": "ffmpeg", "found": ok})
    if provider_type == "mlx-whisper":
        ok = hardware.apple_silicon and hardware.mlx_whisper_installed
        return ("healthy" if ok else "unavailable",
                {"apple_silicon": hardware.apple_silicon,
                 "runtime_installed": hardware.mlx_whisper_installed})
    if provider_type == "faster-whisper":
        ok = hardware.faster_whisper_installed
        return ("healthy" if ok else "unavailable",
                {"runtime_installed": ok, "nvidia_runtime": hardware.nvidia_runtime,
                 "note": "CUDA usability requires a model smoke test"})
    if provider_type == "whispercpp":
        ok = hardware.whispercpp_installed
        return ("healthy" if ok else "unavailable", {"binary_found": ok})
    if provider_type == "pyannote":
        ok = hardware.pyannote_installed
        return ("degraded" if ok else "unavailable",
                {"runtime_installed": ok,
                 "note": "model terms, token, and model access require an authenticated smoke test"})
    if provider_type in {"openai", "deepgram", "assemblyai", "anthropic"}:
        from .routing import _resolve_secret_ref

        configured = bool(secret_ref and _resolve_secret_ref(secret_ref))
        return ("degraded" if configured else "unavailable",
                {"secret_configured": configured,
                 "note": "credentials are present but no paid request was sent"})
    if provider_type in {"ollama", "local"}:
        return ("degraded", {"note": "runtime/model health is checked by localplaud doctor"})
    if provider_type == "remote-worker":
        if not base_url or not secret_ref:
            return "unavailable", {"note": "worker URL and token reference are required"}
        from .remote_worker import RemoteWorkerClient
        from .routing import _resolve_secret_ref

        token = _resolve_secret_ref(secret_ref)
        if not token:
            return "unavailable", {"note": "worker token reference is not available"}
        client = RemoteWorkerClient(base_url, token, timeout=10)
        try:
            handshake = client.handshake()
            return "healthy", {
                "protocol_version": handshake.protocol_version,
                "worker_id": handshake.worker_id,
                "capabilities": [item.model_dump(mode="json")
                                 for item in handshake.capabilities],
                "max_concurrent_jobs": handshake.max_concurrent_jobs,
                "runtime": handshake.runtime,
            }
        except Exception as exc:  # noqa: BLE001 - health boundary reports, never raises
            return "unavailable", {"note": f"worker handshake failed: {str(exc)[:300]}"}
        finally:
            client.close()
    return "unknown", {"note": "no built-in health checker for this provider type"}
