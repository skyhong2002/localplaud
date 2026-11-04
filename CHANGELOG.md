# Changelog

All notable changes to localplaud are documented here. The project follows
[Semantic Versioning](https://semver.org/); until 1.0, minor versions may include
breaking configuration or schema changes, which are called out explicitly.

## [0.1.0] - 2026-11-04

First public release. localplaud replaces the Plaud Intelligence workflow after
raw-audio upload. Everything from transcription onward runs on infrastructure you
choose.

### Capture and sync
- Read-only Plaud Open API sync (OAuth with PKCE). The first sync catalogs the
  library without downloading it; new uploads download automatically.
- Direct audio upload in the Web App for recordings from any source.
- SSRF-checked, size-bounded downloads; Plaud is never modified.

### Speech
- Whisper large-v3-turbo through MLX (Apple Silicon) or faster-whisper (CUDA/CPU),
  plus whisper.cpp, remote-worker, and opt-in cloud providers.
- pyannote speaker diarization with stable speaker IDs, renaming, and per-segment
  reassignment; missing diarization is shown as a degraded state.
- Optional VAD and WhisperX forced alignment.
- Optional cross-recording voice identification from local voiceprints, with a
  calibrated confidence policy.
- Separate raw and corrected transcript revisions. Edits re-index without
  re-running ASR.

### Knowledge
- User-manageable note templates with full-transcript (map-reduce) coverage and
  multiple notes per recording, each with provenance and revisions.
- Mind maps with PNG export, and AI or time-section chapter outlines.
- Single-recording and library-wide Ask, with citations that open playable
  timestamps; answers can be saved as editable notes.
- Full-text search across titles, transcripts, and notes, plus optional embeddings.
- Find and replace across a transcript and its notes.

### Workspace
- A responsive Web App for desktop and mobile with folders, tags, filters, trash
  recovery, and an English or Traditional Chinese (Taiwan) interface.
- AutoFlow rules for templates, processing profiles, organization, exports,
  webhooks, and email delivery.
- Exports: TXT, SRT, VTT, DOCX, and PDF transcripts with timestamp and speaker
  toggles; Markdown, TXT, DOCX, and PDF notes; audio; mind-map PNG; library ZIP
  (up to 50 recordings); revocable public share links.
- Google and local accounts with private per-account workspaces, administrator
  approval, and revocable sessions.
- Durable, independently resumable processing stages with stage-level status,
  provider, model, and error history.
- Workspace backups that exclude secrets and Plaud tokens.

### Security
- All open dependency advisories resolved at release time.

[0.1.0]: https://github.com/skyhong2002/localplaud/releases/tag/v0.1.0
