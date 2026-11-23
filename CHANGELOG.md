# Changelog

All notable changes to localplaud are documented here. The project follows
[Semantic Versioning](https://semver.org/); until 1.0, minor versions may include
breaking configuration or schema changes, which are called out explicitly.

## [0.3.0] - 2026-11-23

- Fixed library and recording-picker search for generated titles while retaining
  original-filename matching and private workspace boundaries.
- Kept public-share audio playback available when the transcript is not shared.
- Added foreground Android USB/BLE diagnostics. Finding a recorder does not grant
  permission to connect or read it; the release APK keeps direct connection gated.
- Added a separately installed, Debug-only BLE Lab APK and a reusable BlueZ test
  peripheral for connection, bounded recovery, and automatic-reconnection testing.
  Pixel 7 completed three peripheral disconnect/reconnect cycles without another
  tap. These tests use a custom test service, not a Plaud recorder.
- Added upload-only, short-lived browser approval and idempotent audio-ingest
  receipts, plus a durable Android upload queue and manual Debug upload diagnostics.
  Recorder transfer and background automatic upload are not implemented.
- Added Android variant tests, lint, and packaged-manifest/isolation checks to CI
  and release builds. Releases include the signed workspace APK and a clearly
  named, separate Debug BLE Lab APK, alongside Python packages and checksums.
- Expanded regressions for missing speech dependencies recovering without losing
  transcripts or edits. Real-model and authenticated production acceptance remain
  tracked separately from synthetic tests.

Original Plaud Note direct connection, recording access, and automatic transfer
remain unverified and require supported device authorization. Existing recorder
bindings and recordings are preserved. See the
[Android limitations](https://github.com/skyhong2002/localplaud/blob/v0.3.0/docs/android.md).

## [0.2.0]

- Added an optional Android app for the self-hosted workspace, with a launcher
  shortcut to the official Plaud app for recorder uploads.
- Added **Check Plaud now** to Home and the phone's Add menu to find raw audio
  already uploaded to Plaud Cloud and show what is queued for local download.
- Refined recording navigation and playback, added a calendar, recent speaker
  names, note-generation actions, grouped exports, and recoverable phone
  recording drafts after stopping.
- Added GitHub release automation for Python packages and optional signed APKs.
  The Android build is tied to a configured workspace origin and does not pair
  with the recorder or replace the official upload path.

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
