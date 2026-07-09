# ADR 0003: Pluggable ASR — provider registry with ordered fallback

Status: Accepted

## Context

No single ASR setup fits every deployment: a Mac mini wants Metal
(whisper.cpp / mlx-whisper), an NVIDIA box wants faster-whisper on CUDA, a
weak VPS is better served by a cloud API, and users differ on the
privacy/accuracy/cost trade-off. Some cloud providers (Deepgram,
AssemblyAI) also return speaker labels server-side, which local Whisper
does not. The rest of the pipeline (diarization, summaries, chunking,
search) must not care where a transcript came from.

## Decision

- A **provider registry** (`localplaud/asr/registry.py`): each provider
  module registers a factory by name via `@register(name)`. Modules are
  imported lazily, so optional heavy deps (torch, MLX, cloud SDKs) are
  only required for the provider actually selected.
- **Local and cloud providers are equal first-class choices**, selected by
  `asr.provider` in config — cloud is a deliberate option (accuracy,
  server-side diarization, no GPU needed), not merely a weak-machine
  fallback. Supported: `faster-whisper`, `whispercpp`, `mlx-whisper`
  (local); `openai`, `deepgram`, `assemblyai` (cloud).
- **Ordered fallback**: `asr.fallback` lists providers tried in order when
  the primary reports `available() == False` or raises `AsrUnavailable`
  (missing GPU, model, dependency, or API key).
- **One normalised output**: every provider returns the
  `Transcript`/`Segment`/`Word` model (`localplaud/asr/base.py`) with
  timestamps and optional speaker labels. `Transcript.has_speakers`
  records whether diarization already happened; if not, the pyannote
  stage fills in speakers afterwards.

## Consequences

- Adding a provider is one module implementing `available()` +
  `transcribe()` and normalising to `Transcript`; nothing downstream
  changes.
- `pip install localplaud` stays lightweight; extras
  (`faster-whisper`, `mlx`, `cloud`, `diarize`) pull deps per provider.
- Fallback makes the pipeline resilient (e.g. local model missing →
  cloud), at the cost that a misconfigured primary can silently shift
  work to a paid provider — fallback use is logged at WARNING.
- Normalisation flattens provider-specific extras (per-word confidence
  variants, punctuation metadata) to the common model; anything else must
  be added to the shared model deliberately.
