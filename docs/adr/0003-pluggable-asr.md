# ADR 0003: SOTA local speech pipeline with pluggable providers

Status: Accepted (revised 2026-07-10)

## Context

localplaud must replace Plaud's paid transcription and speaker workflow from raw
audio. A provider abstraction is still useful across Apple Silicon, NVIDIA, CPU, and
explicit cloud configurations, but merely returning transcript text is not enough.
The default product result needs strong multilingual ASR, accurate timestamps, and
speaker-attributed segments suitable for editing, playback, summaries, and citations.

Whisper does not perform speaker diarization. Describing a Whisper model as
"speaker-aware" conflates separate problems and produces a misleading product state.

## Decision

- The default accuracy/speed baseline is **OpenAI Whisper large-v3-turbo**:
  - Apple Silicon: `mlx-community/whisper-large-v3-turbo` via MLX Whisper.
  - NVIDIA/CUDA and CPU: `large-v3-turbo`/`turbo` through faster-whisper or an
    equivalent verified CTranslate2 conversion.
- The complete default speech path is:

  ```text
  VAD -> Whisper large-v3-turbo -> word-level alignment
      -> speaker diarization -> word/segment speaker assignment
  ```

- Use a current production-quality pyannote pipeline, or a replacement that wins a
  documented benchmark. WhisperX is an acceptable integration pattern for alignment,
  diarization, and speaker assignment.
- Diarization is required for the Plaud-like default profile. If it cannot run, store
  the ASR artifact but mark the recording clearly degraded; never set
  `has_speakers=true` without real speaker output.
- Store stable machine speaker IDs separately from editable display names. Preserve
  timestamps, word data, provider/model/version, language, and confidence where
  available.
- Keep the provider registry and normalized transcript contract. Local turbo is the
  subscription-independent default; cloud providers are explicit operator choices,
  not silent fallbacks. Any paid fallback must require opt-in and be visible in UI,
  logs, and artifact provenance.
- Taiwan Mandarin and Mandarin/English code-switching are first-class benchmark cases.
  Model or diarization changes require evaluation on consented user-owned recordings
  for WER/CER, diarization error, hallucination, timestamp quality, speed, and memory.

## Consequences

- Existing providers may remain available, but not every provider satisfies the full
  Plaud-like quality profile.
- The current `transcribe -> diarize` implementation is an intermediate state; word
  alignment, durable stage runs, editable speakers, and degraded-state UI are required.
- Apple Silicon and CUDA use different inference runtimes while targeting the same
  model family and normalized artifact.
- Speaker diarization adds model downloads, compute, and potentially a Hugging Face
  token/license acceptance step. Deployment and health checks must surface this.
- Falling back from local inference to a paid API can break the project's cost/privacy
  promise, so availability alone is insufficient justification for automatic fallback.
