# localplaud — status & TODO

Working notes for continuing development (synced across machines via git).
No secrets here — those live in `.env` / the Caddyfile, never committed.

Audited 2026-08-19 against the running production system, the production
database, and the codebase: every open item below was re-verified with
evidence; completed work moved to the compressed archive at the bottom.
Full pre-audit engineering detail lives in this file's git history.

## Status snapshot (2026-08-19)

- Full app built & published: <https://github.com/skyhong2002/localplaud> (MIT).
  Active development is merged directly to `main` (test count verified per change).
- **Production is LIVE** on the M4 Mac mini (hostname now `sky-mini`, formerly
  SkyLabMac/CCLabMacmini): launchd agent `com.localplaud.agent`, reverse-proxied
  at **https://plaud.observe.tw** (healthz 200 verified 2026-08-19).
- **Independent mode runs on execution profile `mac-wsl-hybrid` v11** (system
  default, operator-configured in the DB): Mac MLX Whisper large-v3-turbo ASR
  with WhisperX/wav2vec2 forced alignment; diarize / summarize / mind-map / embed
  dispatched to the **WSL RTX 5060 remote worker** over Tailscale; transcript
  polish and Ask currently use the Mac's local `qwen3.5:9b` through Ollama.
- **WSL CUDA ASR is verified but intentionally non-default.** A real 43-second
  recording completed on the RTX 5060 with faster-whisper `large-v3-turbo`
  (16.063 s, observed 68% GPU / 3.74 GB VRAM) and then passed the complete
  12-check pipeline. The same short file took 5.559 s on Mac MLX, so the
  production default remains Mac MLX until a representative long-file benchmark
  demonstrates a throughput win.
- **Production authentication is enabled.** Caddy terminates HTTPS and
  localplaud owns the password/session login; credentials and session secret are
  stored only in the ignored mode-0600 `.env` and macOS Keychain. Anonymous
  browser traffic is redirected to `/login`, API-style traffic fails with 401,
  and `/healthz` remains available to monitoring.
- **Backlog** (production DB, 2026-08-19, before the repaired queue is resumed):
  234 done · 45 partial · **560 error** · 5 metadata-only. All 123 current
  generated/saved-note knowledge documents are indexed (0 pending/failed).
  The WSL GPU worker is the throughput bottleneck; `concurrency = 1` on the
  16 GB Mac is deliberate (2× Whisper pushed it into swap).
- **Large-library controls are lazy-loaded.** The homepage no longer renders
  562 tag buttons and every organization row up front; the real production HTML
  fell from 1,456,500 to 430,698 bytes while tag filtering and bulk organization
  remain available on demand.
- Legacy DB migration (note_templates / vocabulary_terms / stage_attempts /
  ask_messages) completed 2026-07-13 with verified row counts and integrity
  checks; details in `CONTINUATION.md` git history.

## Open TODO — prioritized

### P0 — validation debt (quality gates before changing defaults)

- **WhisperX forced-alignment validation.** `align:whisperx` /
  `wav2vec2-auto` are now the production default. A real 43-second Mandarin
  recording passed all 12 independence checks on 2026-08-19. Two previously
  failing long recordings also completed after empty-placeholder hardening: one
  aligned 8,879 words at 100% segment coverage and the other aligned 6,771 words
  across all 169 non-empty segments while preserving one empty bookkeeping
  placeholder. Broader Taiwan Mandarin and Mandarin/English accuracy, timestamp,
  speed, and memory benchmarking is still required before considering this
  quality validation complete.
- **VAD validation.** `asr.vad.enabled` remains default-off (implementation
  is complete for both mlx and faster-whisper paths). Benchmark on real
  Taiwan Mandarin / code-switch recordings before enabling by default. No
  benchmark harness exists anywhere in the repo — building one is part of
  this item (it also unblocks the two items above).
- **Cross-host artifact contract for the live Mac↔WSL pair.** Per-artifact
  SHA-256 verification is tested, but nothing compares the same recording's
  artifacts produced on the two production hosts —
  `docs/product-workflow.md` acceptance scenarios 8 and 12 have no executable
  form. (Rentable-GPU host validation was dropped with the 2026-07-31
  single-deployment decision below.)

### P0 — operations

- **Drain the repaired backlog.** The 2026-08-19 repair restored the missing
  worker secret, rotated it, synchronized the WSL worker, and proved short and
  long real end-to-end recordings. Requeue the remaining 560 error and 45 partial rows;
  processing remains bounded by the WSL GPU and fresh uploads stay ahead of
  historical work by design.

### P1 — Web App remaining gaps (2026-07-31 audit vs `docs/product-workflow.md`)

- **Settings resolution-preview UI.** Headless `POST /api/providers/resolve`
  exists and the recording detail page shows the resolved layer chain, but
  Settings itself never calls the preview API.
- **Cost-ceiling display + version-prefill bug.** `cost_ceiling` is a form
  input but is never rendered on existing profiles, and the "New version"
  prefill drops `cost_ceiling` and `is_system_default` — versioning a
  cost-capped profile silently removes its cap.
- **Remote-worker management detail.** Settings renders only
  name/key/protocol/token-env/health. Product spec wants capability,
  device/memory, queue, last health-check time, and revocation state; the
  protocol `HandshakeResponse` needs those fields first (today only a
  free-form metadata dict).
- **Tags in the persistent sidebar.** Folders and Sources have nav groups;
  tags are only a filter row on the library page.
- **Per-file Custom mode.** Per-recording language and speaker-count
  overrides (today `num_speakers` is global config only; the detail page has
  no language control).
- **Cloud/remote starting profiles.** Only the three local hardware
  recommendations (apple-mlx, nvidia-cuda, cpu) ship; no OpenAI Cloud /
  OpenAI-compatible / Remote GPU starting profile flow.
- **Storage use + retention settings.** Backup, auth, and privacy surfaces
  exist; storage-use display and retention policy do not.
- **Quality-floor fallback policy.** Capability, no-egress, and cost-ceiling
  constraints are enforced; the quality floor from the policy spec is
  unimplemented.
- **AutoFlow next-run display.** Discover renders run_count/last_run only.
- **Fresh read-only Plaud Web comparison** for the selected-recording and
  mobile views (last screenshot-led fidelity pass was 2026-07-18; polish-loop
  UX iterations continue in `.agents/polish/backlog.md`).
- Optional: distinct Summary tab (tracked in the polish backlog) and
  community/remote template-catalog ingestion.

### ~~P1 — Multi-host deployment~~ — DROPPED (decision 2026-07-31)

Multi-host web deployments are no longer a goal: the CCLabPC
(nvplaud.observe.tw) and Oracle (plaud.skyhong.tw) standalone instances are
retired, and rentable-GPU validation is out of scope. Production is exactly
one Mac mini controller plus its private WSL RTX 5060 worker; that topology
(dispatch stages, GPU serialization lock, code-sync and remote_jobs cache
caveats) is now documented in `docs/remote-worker.md`, and `docs/deploy.md`
keeps only generic `cpu`/`gpu` profile instructions for other users.
Follow-up when convenient: remove the two stale DNS records / Caddy vhosts on
the retired hosts.

### P2 — Automation and integrations

- Concrete application adapters beyond the generic external-owner rule
  contract (the Applications & Integrations catalog, external-rule read-only
  ownership, and every planned downstream action type are done).

### Housekeeping

- Optional: root LaunchDaemon so production starts on boot without login
  (needs sudo; confirmed absent 2026-07-31 — only the per-user LaunchAgent
  exists).

## ✅ Done — capability archive (compressed 2026-07-31)

Each area below is complete and verified; per-item engineering notes are in
this file's git history (pre-2026-07-31 versions).

- **Plaud ingest foundation.** Official Open API provider with native S256
  PKCE OAuth (loopback flow, auto-refresh, CLI-compatible tokens) plus the
  official Plaud MCP as a second read-only provider (production currently
  runs `provider = "mcp"`). Signed raw-audio download with SSRF/size
  protections. Plaud transcripts/summaries are migration/debug-only imports,
  visibly labelled, never a pipeline dependency.
- **Production-safe independent processing.** `artifact_mode = "independent"`
  default; provenance-preserving multi-row transcript storage; durable
  per-stage runs with attempt counts and actionable partial states; bounded
  exponential auto-retry; newest-first bounded queue; baseline-aware catalog
  sync (no surprise historical backfill) with durable download leases; the
  read-only twelve-part `acceptance-check` gate surfaced in CLI, API, and the
  recording workspace.
- **Provider/model/profile platform.** Capability contracts for every stage;
  durable connections/models/profiles with secret references only; layered
  deterministic resolution (system → folder → AutoFlow → template →
  recording) with immutable per-run snapshots; full CRUD + real health
  checks; truthful hardware detection with one-click local profile install;
  append-only usage/cost ledger with pre-egress cost-ceiling reservations;
  authenticated `localplaud-worker` protocol v1 (idempotent jobs, progress,
  cancellation, SHA-256 artifacts, credential rejection); stage-scoped
  explicit cross-provider fallback; the experimental codex-local correction
  adapter — now live in production as the correct-stage fallback on profile
  `mac-wsl-hybrid` v7 with completed real attempts.
- **Speech and speakers.** MLX Whisper large-v3-turbo on Apple Silicon;
  pyannote `speaker-diarization-community-1` with explicit device selection
  and real production completions; the durable `align` stage (honest
  `provider-word-timestamps` labelling) plus the selectable `align:whisperx`
  forced-alignment provider; VAD groundwork behind the default-off flag;
  stable speaker IDs with renames, one-to-one rerun reconciliation,
  segment-level attribution correction, and Plaud-style speaker paragraphs;
  the durable vocabulary/correction layer applied as immutable revisions.
- **Notes and knowledge.** Contextual transcript polish (OpenCode Go) as an
  immutable canonical revision with empty-segment rejection and repair;
  full-coverage map/reduce summaries and mind maps (collapsible tree, PNG
  export); versioned editable note templates with deterministic Auto
  selection; single-file and whole-library Ask with playable citations,
  durable scoped threads, history drawer, quick actions, suggested
  questions, and save-to-note; fail-closed embedding identity with the
  durable note-embedding queue; one shared cost ledger across pipeline,
  indexing, and Ask; transcript corrections as immutable revisions with
  find/replace, bulk edits, history, and non-destructive restore; manual and
  editable-copy notes with immutable version history.
- **Web App.** Two screenshot-led Plaud-fidelity passes (2026-07-18 shell:
  single white sidebar, breadcrumb workspace, Ask dock); original brand
  system (`docs/brand.md`, logo/wordmark/favicon, CSS token set, OFL Noto
  Sans TC, vendored HTMX/Lucide, no CDN); Home dashboard; library sorting,
  processing/source/date/duration filters, folders/tags with bulk
  operations, trash mirror, bulk Resume/cleanup; Add audio (upload incl.
  .amr + durable Plaud import); persistent waveform player with deep-link
  seek; local-data lifecycle controls; durable local title overrides + LLM
  title generation; lexical+semantic search landing on the exact moment;
  scoped whole-library Ask; Templates My Space/Explore; consolidated exports
  (TXT/SRT/VTT/DOCX/PDF, notes, archive ZIP, mind-map PNG, audio) with
  copy-to-clipboard; public share links with minimap and note pager;
  read-only cloud-artifact mirroring with 「重新整理 Plaud 雲端資料」;
  auto-tagging (typed topic/person/org); zh-Hant-TW locale across shell,
  surfaces, and dynamic template messages with a literal-English guard test.
- **Automation and settings.** Executable AutoFlow rules (source/title/
  duration/folder/tag/early-transcript triggers; profile/template/
  organization/export/webhook/SMTP actions; durable runs, retries,
  notifications inbox); Discover hub with external-owner read-only rules;
  Settings IA covering account/auth sessions, workspace preferences,
  locale, vocabulary, templates, providers/profiles, remote workers,
  integrations, private backup (online SQLite backup + cross-host upload),
  diagnostics bundle, and system health.
- **Post-2026-07-18 work previously unrecorded here** (from git log): the
  mac+WSL worker pipeline with GPU-stage serialization and the remote embed
  model-attest fix; whole-library reprocess-all; LLM title generation;
  typed auto-tags; .amr upload support; share-page transcript minimap and
  note pager; named capture sources incl. zh sidebar rendering;
  Traditional-Chinese output enforcement for generated notes; ~40+
  polish-loop UX/localization fixes (mobile sticky player, reload-restore,
  scroll shadows, aria-label localization, favicon, bulk-bar layout, …).

## Ops quick-reference (sky-mini, a.k.a. SkyLabMac)

- Update prod: `git -C ~/Projects/localplaud pull && launchctl kickstart -k gui/$(id -u)/com.localplaud.agent`
  (production serves directly from this checkout; a commit alone does not
  restart the service)
- Logs: `~/Projects/localplaud/data/service.{out,err}.log`
- Service: `launchctl list | grep localplaud`; plist at `~/Library/LaunchAgents/com.localplaud.agent.plist`
- Caddy vhost: block for `plaud.observe.tw` in `/usr/local/etc/caddy/Caddyfile`;
  Caddy terminates HTTPS while localplaud owns `/login` and browser sessions.
- Session/creds: `~/Projects/localplaud/.env` (git-ignored)
