"""Truthful built-in capability declarations, independent per API shape."""

from __future__ import annotations

from .contracts import CapabilityKind, CapabilitySpec, EgressPolicy

BUILTIN_CAPABILITIES = {
    "mlx-whisper": [CapabilitySpec(CapabilityKind.asr, timestamps=True,
                                    word_timestamps=True, required_hardware=("apple_silicon",))],
    "faster-whisper": [CapabilitySpec(CapabilityKind.asr, timestamps=True,
                                       word_timestamps=True, required_hardware=("cpu_or_cuda",))],
    "whispercpp": [CapabilitySpec(CapabilityKind.asr, timestamps=True,
                                   required_hardware=("cpu",))],
    "pyannote": [CapabilitySpec(CapabilityKind.diarization, speaker_output=True,
                                 required_hardware=("torch",))],
    "whisper-timestamps": [CapabilitySpec(CapabilityKind.alignment, timestamps=True,
                                           word_timestamps=True)],
    "whisperx": [CapabilitySpec(CapabilityKind.alignment, timestamps=True,
                                 word_timestamps=True, required_hardware=("torch",))],
    "ollama": [CapabilitySpec(CapabilityKind.notes), CapabilitySpec(CapabilityKind.mind_map),
                CapabilitySpec(CapabilityKind.ask), CapabilitySpec(CapabilityKind.correction)],
    "anthropic": [CapabilitySpec(CapabilityKind.notes, egress=EgressPolicy.external),
                   CapabilitySpec(CapabilityKind.mind_map, egress=EgressPolicy.external),
                   CapabilitySpec(CapabilityKind.ask, egress=EgressPolicy.external),
                   CapabilitySpec(CapabilityKind.correction, egress=EgressPolicy.external)],
    "local": [CapabilitySpec(CapabilityKind.embeddings)],
    # These declarations are intentionally separate. A compatible server may
    # expose any subset; callers must never infer the other shapes.
    # Conservative across OpenAI audio models: whisper-1 can return word
    # timestamps, while GPT-4o transcription models expose different shapes.
    "openai-audio": [CapabilitySpec(CapabilityKind.asr,
                                     egress=EgressPolicy.external)],
    "openai-text": [CapabilitySpec(CapabilityKind.notes, egress=EgressPolicy.external),
                     CapabilitySpec(CapabilityKind.mind_map, egress=EgressPolicy.external),
                     CapabilitySpec(CapabilityKind.ask, egress=EgressPolicy.external),
                     CapabilitySpec(CapabilityKind.correction, egress=EgressPolicy.external)],
    "openai-embeddings": [CapabilitySpec(CapabilityKind.embeddings,
                                          egress=EgressPolicy.external)],
    "codex-local": [CapabilitySpec(CapabilityKind.notes, egress=EgressPolicy.external),
                     CapabilitySpec(CapabilityKind.mind_map, egress=EgressPolicy.external),
                     CapabilitySpec(CapabilityKind.ask, egress=EgressPolicy.external),
                     CapabilitySpec(CapabilityKind.correction, egress=EgressPolicy.external)],
    "remote-worker": [
        CapabilitySpec(CapabilityKind.asr, timestamps=True, word_timestamps=True,
                       egress=EgressPolicy.trusted_worker),
        CapabilitySpec(CapabilityKind.alignment, timestamps=True, word_timestamps=True,
                       egress=EgressPolicy.trusted_worker),
        CapabilitySpec(CapabilityKind.diarization, speaker_output=True,
                       egress=EgressPolicy.trusted_worker),
        CapabilitySpec(CapabilityKind.correction, egress=EgressPolicy.trusted_worker),
        CapabilitySpec(CapabilityKind.notes, egress=EgressPolicy.trusted_worker),
        CapabilitySpec(CapabilityKind.mind_map, egress=EgressPolicy.trusted_worker),
        CapabilitySpec(CapabilityKind.embeddings, egress=EgressPolicy.trusted_worker),
        CapabilitySpec(CapabilityKind.ask, egress=EgressPolicy.trusted_worker),
    ],
    "deepgram": [CapabilitySpec(CapabilityKind.asr, timestamps=True, word_timestamps=True,
                                 speaker_output=True, egress=EgressPolicy.external)],
    "assemblyai": [CapabilitySpec(CapabilityKind.asr, timestamps=True, word_timestamps=True,
                                   speaker_output=True, egress=EgressPolicy.external)],
}


def specs_for_route(provider: str, stage: str) -> list[CapabilitySpec]:
    alias = provider
    if provider == "openai":
        alias = {"transcribe": "openai-audio", "summarize": "openai-text",
                 "mind_map": "openai-text", "ask": "openai-text",
                 "correct": "openai-text", "index": "openai-embeddings"}.get(stage, provider)
    return BUILTIN_CAPABILITIES.get(alias, [])


_KNOWN_LOCAL = {"ffmpeg", "mlx-whisper", "faster-whisper", "whispercpp", "pyannote",
                "ollama", "local", "local-rules", "whisper-timestamps", "whisperx", "none"}


def provider_egress(provider: str, stage: str) -> EgressPolicy:
    specs = specs_for_route(provider, stage)
    if specs:
        if any(spec.egress == EgressPolicy.external for spec in specs):
            return EgressPolicy.external
        if any(spec.egress == EgressPolicy.trusted_worker for spec in specs):
            return EgressPolicy.trusted_worker
        return EgressPolicy.local_only
    if provider in _KNOWN_LOCAL:
        return EgressPolicy.local_only
    if provider == "remote-worker":
        return EgressPolicy.trusted_worker
    # Unknown/custom providers are conservatively external until a durable
    # connection is materialized with an authoritative type.
    return EgressPolicy.external
