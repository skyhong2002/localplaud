# localplaud — status & TODO

Working notes for continuing development (synced across machines via git).
No secrets here — those live in `.env` / the Caddyfile, never committed.

Audited 2026-08-19 against the running production system, the production
database, and the codebase: every open item below was re-verified with
evidence; completed work moved to the compressed archive at the bottom.
Full pre-audit engineering detail lives in this file's git history.

## Mini processing recovery (2026-10-02)

- Implemented orphan stage roll-up recovery, preserving live claims and artifacts.
- Restricted new-recording mode now resumes recent transcript/speaker reindexes;
  historical backfills and stale-note regeneration remain deliberate operations.
- Scoped pipeline fixes were deployed after checking for active work; local and
  public health returned 200. A recent stale index resumed with existing transcript,
  speaker and note hashes unchanged. Historical quota failures were not drained.
- Two priority recordings passed all 13 read-only artifact-independence checks.
  The recovered recording's alignment timestamps matched its preservation backup;
  guarded metadata repair restored overwritten forced-alignment provenance.
  This gate checks indexed artifacts, not generated Ask answer quality.
- One additional designated derived-only repair completed with speech/user-edit
  hashes unchanged; its artifact gate is 12/13 because raw-audio cache was evicted.
  The other designated recording is still correcting. Do not restart active work.
  The alignment source fix is installed on
  disk; already-running processes retain their previous imported code until a
  safe reload.
- Voice matching must retain anonymous labels when evidence is insufficient or
  below threshold; service enablement alone is not proof of a successful match.

## Finish sprint (2026-10-02, `integration/finish-20261002`)

Deployed to production on 2026-10-02 18:24 Taipei as a reviewed source patch
(release `48dbcb79`), after a dry run on a copy of production data: startup with no
tracebacks, core pages 200, 0 of 8,666 artifact-reuse checks changed, rollback
release readable after migration. Restart took 93 s; transcript, revision, note
and speaker table hashes were identical before and after. The voice-identity
service was restarted on the new code. The WSL worker remains on its previous code.
New note generations re-run from scratch once because the evidence-note system
prompt now asks the model to keep `Speaker N` labels (part of the checkpoint key).

- [x] Voice-similarity name suggestions: below-threshold or ambiguous candidates
  appear in Name speakers as suggestions the user confirms with Save. Thresholds
  are unchanged, nothing is auto-applied, and a confirmation is recorded as
  `user_confirmed` with a `suggestion_confirmed` event.
- [x] Settings resolution preview, storage use and backup retention, remote-worker
  runtime detail, opt-in cloud/remote starting profiles.
- [x] Sidebar tags, AutoFlow next-run wording, truthful template attribution,
  per-recording speech settings, workspace QA follow-ups.
- [x] Quality-floor fallback policy, remote-worker outline dispatch, and the
  `localplaud benchmark-speech` harness (no real-recording benchmark run yet).
- Deploy order: upgrade the controller first. A previous-release controller cannot
  parse the `outline` capability a new worker advertises, so keep the WSL worker
  on its current code until the controller release is settled; until the worker is
  updated it does not report runtime detail, apply speech overrides or run outline.
- Open: a revoke/re-enable action for remote workers; speaker-count overrides are
  honoured by pyannote only; Settings storage measurement is untimed on the
  production library; real-model chapter quality and live Ask answer quality
  remain unvalidated.

## Speaker naming workflow (2026-10-02 integration)

- [x] Friendly anonymous Speaker 1/2 labels with stable internal speaker keys.
- [x] One atomic Name speakers save updates transcript labels and bound mentions
  in existing generated notes/mind maps, including exports, without regenerating.
  Prior note versions are archived; user notes and raw transcripts are preserved.
- [x] Auto voice assignment and undo use the same note-name projection.
- [x] Stale generated notes remain readable with an explicit out-of-date status.
- [x] Legacy ambiguous personal names remain unchanged with a persistent review
  notice. A missing old speaker association is not guessed from ordinary prose.
- Browser acceptance and deployment remain tracked in the integration report;
  these changes have not been applied to production.

## Empty share sheet follow-up (2026-09-26)

- Android Brave follow-up: Fanboy Social's generic `##.share-body` cosmetic
  filter matched the entire dialog body, leaving its title visible. Replace that
  generic social-widget class with `lp-recording-actions-body`; version both
  assets. The v3 delegated handler takes over retained v2 listeners during HTMX
  navigation without reloading the page or discarding edits. This addresses a
  different cause than the earlier sheet-height fix. Browser verification uses
  the blocking rule itself: actual desktop Brave reproduced the blank body and
  recovered with the rule still active. Sixteen viewport/state combinations
  passed (ready/empty/loading/error; mobile/landscape/desktop), as did all 55 Web
  UI tests. Browser interactions also verified create/copy/revoke links, actual
  clipboard and downloads, HTMX library navigation, API failure/retry, WebKit,
  and v3 takeover with reconstructed v2 listeners. A physical Android Brave
  check is still unavailable.

Processing recovery follow-up: Qwen's Korean forced-alignment path failed because
the isolated speech environment lacked `soynlp`. The speech image now pins Korean
and Japanese tokenizer dependencies and runs both during its build check. A scoped,
private recovery runner supports provider-quota waits, model-migration checks, and
bounded per-recording retries; see [processing-repair.md](docs/processing-repair.md).
Backlog completion and note-quality validation remain pending while recovery runs.
The operator-confirmed Qwen/Nemotron/GPT-6 Sol selections resolve consistently for
all 946 non-trash recordings; 374 older per-recording overrides were migrated.
The 343-entry recovery manifest runs under launchd. A priority recording now
completes Qwen ASR and Nemotron diarization, but retains six unsupported-language
alignment spans and waits for the selected text provider's quota. These are still
open issues, not successful end-to-end completion.

- The user screenshot showed a sheet reduced to its handle/title. Share sizing is
  now self-contained: every recording fragment loads its own stylesheet, the
  dialog has an explicit viewport-bounded height, and its body scrolls separately.
  This also prevents an older HTMX shell stylesheet from sizing the new content.
- Verified actual content heights and reachable actions in Chromium/WebKit at
  390×844, 360×640, 1440×900 and 844×390, including internal scrolling, Back,
  Escape and old-shell navigation. The old-shell simulation did not reproduce
  zero height; it verifies compatibility rather than proving the device cause.

## Unified recording share sheet (2026-09-25 follow-up)

- Replaced separate Share/Export dialogs and header buttons with one grouped sheet:
  public link, clipboard transcript/individual notes/all notes, and file formats
  for audio/transcript/notes/mind map. Back navigation stays inside the sheet.
- Sharing initializes independently of the transcript/player script and delegates
  actions across progressive navigation and history restoration. Link errors offer
  Retry, pending requests abort on close, and clipboard writes start within the
  user gesture for Safari. Public sharing remains explicitly opt-in.
- Verified 68 API/render tests, desktop/mobile link and clipboard/export flows,
  Chromium and WebKit library navigation/history restoration, injected workspace
  failures and Retry, plus read-only Share open/close on live recordings.

## Recording workspace repair (2026-09-25)

- Fixed collapsed share/export dialog bodies, moved public-link creation and copy
  ahead of the authenticated workspace link, and routed native sharing to the
  public URL. Notes now prioritize the complete title and content; generation
  controls expand on demand. Mobile playback stays at the bottom, More stays
  within the viewport, and transcript selection survives refresh/Back.
- Regression coverage includes local-only notes, explicit sharing, and empty
  recording generation controls; no Plaud-generated artifact is required.
- Verified with 66 API/render tests and 26 browser checks at 360×740, 390×844,
  and 1440×900, including create/copy/public access/revoke, actual TXT download,
  empty/loading/error states, long notes, and share-request failure recovery.

## Note quality update (2026-09-24)

- Evidence notes v2 adds timestamped facts, extraction/draft verification, bounded
  repairs, private checkpoints, and input-change protection. Existing notes are
  archived on successful replacement; failed checks preserve them.
- Transcript checks block invalid timestamps and long fixed filler loops; warnings
  are not acoustic verification. Bad sources still require audio recovery.
- Real-cohort quality metrics and parity with Plaud remain evaluation targets; see
  [note-quality.md](docs/note-quality.md). No benchmark score is implied by tests.

## Historical status snapshot (2026-08-19)

- Full app built & published: <https://github.com/skyhong2002/localplaud> (MIT).
  Active development is merged directly to `main` (test count verified per change).
- **Production is LIVE** on the M4 Mac mini (hostname now `sky-mini`, formerly
  SkyLabMac/CCLabMacmini): launchd agent `com.localplaud.agent`, reverse-proxied
  at **https://plaud.observe.tw** (healthz 200 verified 2026-08-19).
- **Independent mode runs on execution profile `mac-wsl-hybrid` v12** (system
  default, operator-configured in the DB): WSL RTX 5060 faster-whisper
  large-v3-turbo ASR with Mac WhisperX/wav2vec2 forced alignment; diarize /
  summarize / mind-map / embed
  dispatched to the **WSL RTX 5060 remote worker** over Tailscale; transcript
  polish and Ask currently use the Mac's local `qwen3.5:9b` through Ollama.
- **WSL CUDA ASR is verified and is the production default.** The CUDA 12.8
  image carries cuDNN 9 for torch/pyannote and the side-by-side cuDNN 8 runtime
  required by the pinned CTranslate2 4.4 ASR stack. A fresh real 43-second
  recording completed with faster-whisper `large-v3-turbo` on CUDA, followed by
  Mac WhisperX and WSL pyannote, and passed the complete 12-check independent
  pipeline. The worker remained healthy with zero restarts and no OOM.
- **Production authentication is enabled.** Caddy terminates HTTPS and
  localplaud owns the password/session login; credentials and session secret are
  stored only in the ignored mode-0600 `.env` and macOS Keychain. Anonymous
  browser traffic is redirected to `/login`, API-style traffic fails with 401,
  and `/healthz` remains available to monitoring.
- **Backlog** (production DB, 2026-08-19, while the repaired queue is draining):
  241 done · 45 partial · **552 queued/error** · 5 metadata-only · 2 processing.
  Counts are intentionally transient; the live processing/status surfaces are
  authoritative. All 123 current
  generated/saved-note knowledge documents are indexed (0 pending/failed).
  End-to-end pipeline concurrency remains 1 on the 16 GB Mac; both remote GPU
  stages and long local Ollama transcript-polish stages can bound throughput.
- **Large-library controls are lazy-loaded.** The homepage no longer renders
  562 tag buttons and every organization row up front; the real production HTML
  fell from 1,456,500 to 430,698 bytes while tag filtering and bulk organization
  remain available on demand.
- Legacy DB migration (note_templates / vocabulary_terms / stage_attempts /
  ask_messages) completed 2026-07-13 with verified row counts and integrity
  checks; details in `CONTINUATION.md` git history.

## Open TODO — prioritized

### P0 — validation debt (quality gates before changing defaults)

- **Recording-note fidelity (2026-09-23).** Same-recording comparisons found
  missing substantive topics, repeated filler, incorrect decisions and invented
  actions. Versioned execution instructions, retained Autopilot detail sections,
  explicit Ollama context and truncation rejection are deployed. With explicit
  user authorization, the selected 20 primary Autopilot notes were regenerated
  using the configured Codex model, retaining old notes and recording-scoped
  provenance. A subsequent explicit request migrated all active text-stage settings
  to GPT-6 Sol, including grounded Ask; see [GPT-6 settings](docs/gpt6-settings.md).
  Local-model drafts have not
  passed fidelity review; broader quality evaluation and recovery of inadequate
  transcripts remain open. See
  [note quality](docs/note-quality.md).

- **WhisperX forced-alignment validation.** `align:whisperx` /
  `wav2vec2-auto` are now the production default. A real 43-second Mandarin
  recording passed all 12 independence checks on 2026-08-19. Two previously
  failing long recordings also completed after empty-placeholder hardening: one
  aligned 8,879 words at 100% segment coverage and the other aligned 6,771 words
  across all 169 non-empty segments while preserving one empty bookkeeping
  placeholder. A further two-hour Mandarin meeting aligned 19,703 words across
  all 574 non-empty segments at 100% coverage after preserving a legitimate
  cross-chunk start overlap. Broader Taiwan Mandarin and Mandarin/English
  accuracy, timestamp, speed, and memory benchmarking is still required before
  considering this quality validation complete.
- **VAD validation.** `asr.vad.enabled` remains default-off (implementation
  is complete for both mlx and faster-whisper paths; production enables shared
  Silero on both hosts, with no-speech gating and versioned acoustic recovery
  verified on affected recordings). Benchmark on real
  Taiwan Mandarin / code-switch recordings before enabling by default. The local-only
  `localplaud benchmark-speech` harness now exists with synthetic metric regression
  tests, manifest references, CER/mixed WER/MER, DER, hallucination indicators,
  timestamp deviation, RTF and sampled peak CPU memory. See `docs/speech-quality.md`.
  Real-recording benchmarking has not been run; the validation debt remains open.
- **Cross-host artifact contract for the live Mac↔WSL pair.** Per-artifact
  SHA-256 verification is tested, but nothing compares the same recording's
  artifacts produced on the two production hosts —
  `docs/product-workflow.md` acceptance scenarios 8 and 12 have no executable
  form. (Rentable-GPU host validation was dropped with the 2026-07-31
  single-deployment decision below.)

### P0 — operations

- **September recovery follow-up.** The official MCP recording-data envelope is
  decoded without treating its advisory prose as JSON. Plaud sync errors no
  longer block the local processing queue. MCP `of_` transport IDs resolve to
  the existing recording IDs, preserving local artifacts instead of importing
  duplicates. WSL container CUDA access must be
  verified independently of container uptime; continue monitoring the resumed
  backlog until all stage failures are resolved.
- **Drain the repaired backlog.** The 2026-08-19 repair restored the missing
  worker secret, rotated it, synchronized the WSL worker, and proved short and
  long real end-to-end recordings. The remaining historical error/partial rows
  are now requeued and draining; keep monitoring stage-level progress and retain
  fresh-upload priority over historical work.

### P1 — Web App remaining gaps (2026-07-31 audit vs `docs/product-workflow.md`)

- [x] **Settings resolution-preview UI.** Settings → Resolution preview calls
  `GET /api/providers/resolution-preview` (system/folder/template/recording
  scope) and shows per-stage provider/model, layer chain, fallbacks and egress.
- [x] **Remote-worker management detail.** `HandshakeResponse.runtime` is an
  optional typed object (device, memory, queue depth, version); Settings renders
  capabilities, runtime facts ("not reported" for older workers), last health
  check and revocation (enabled) state. A revoke/re-enable action is not built yet.
- [x] **Tags in the persistent sidebar.** A bounded most-used Tags group with an
  on-demand, filterable full list (`GET /ui/sidebar-tags`); pages never embed
  every tag.
- [x] **Per-file Custom mode.** Speech settings in the recording's More menu store
  durable per-recording language and speaker-count (auto / exact / range)
  overrides in `plaud_files.speech_overrides`, recorded in stage provenance and
  applied on the next explicit rebuild. Saving never discards transcripts or
  edits. An older remote worker ignores them and the result is marked
  `speech_overrides_unconfirmed`.
- [x] **Cloud/remote starting profiles.** OpenAI Cloud, OpenAI-compatible and
  Remote GPU starters (`POST /api/providers/starting-profiles/{kind}`) clone the
  default, replace only their stages, require an egress acknowledgement, accept
  env-var secret references only, and are never made the default.
- [x] **Storage use + retention settings.** Settings → Storage & retention shows
  measured original audio, converted audio, database and backup sizes, and a
  durable "keep newest N workspace backups" policy (KeyValue `storage_retention`)
  applied after each new backup, with an app-dialog confirmation for immediate
  deletions. Original audio, the database and edits are never retention targets.
- [x] **Quality-floor fallback policy.** Durable per-stage floors reject
  lower or unrated fallback models before execution. Immutable resolution snapshots
  and the provider preview API retain stage-scoped rejection reasons. Catalog quality
  ratings require operator evaluation; the policy does not imply benchmark scores.
- [x] **AutoFlow next-run display.** Rules are event-triggered, so cards state
  what triggers the next run (or why a paused/external rule will not run)
  instead of inventing a time.
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
  explicit cross-provider fallback; the experimental codex-local text
  adapter (served production text stages until 2026-10-02; removed 2026-10-03
  in favor of the `ai-gateway` alias provider, historical rows still load).
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

- Production serves directly from this checkout. Do not pull over a dirty tree or
  restart active stage work. Use an isolated, reviewed source diff, a fresh online
  DB backup and source rollback; new UI deployment requires operator approval.
  See `docs/mini-integration-20261002.md` for the current cutover gates.
- Logs: `~/Projects/localplaud/data/service.{out,err}.log`
- Service: `launchctl list | grep localplaud`; plist at `~/Library/LaunchAgents/com.localplaud.agent.plist`
- Caddy vhost: block for `plaud.observe.tw` in `/usr/local/etc/caddy/Caddyfile`;
  Caddy terminates HTTPS while localplaud owns `/login` and browser sessions.
- Session/creds: `~/Projects/localplaud/.env` (git-ignored)
# Recording title quality (2026-09-20)

- [x] Separate title instructions from note-template descriptions; reject template
  prefixes and retry from full-transcript evidence on controller and remote worker.
- [x] Add reviewable, resumable title-only repair plans with input/provenance checks
  and preservation of manual names, notes, audio, and processing state.

### Plaud migration completeness follow-up (2026-09-26)

- [x] Resolve link-only notes, transcripts and outlines through bounded read-only
  storage requests; preserve existing mirrors on failures or partial responses.
- [x] Keep cloud note identities, original tab names, duplicate note types and
  local notes intact across refreshes; expose import directly in an empty workspace.
- [x] Explain unavailable Plaud app-only summary cards rather than rendering dead
  URI text or claiming their images were copied.
- [ ] Exact summary-card parity remains limited by the official interface when it
  supplies no downloadable image. Local-generation quality remains a separate
  benchmark; migration is not a substitute for the independent pipeline.

Acceptance evidence for the migration repair: a read-only six-month audit covered
462 non-trash recordings; 141 supplied notes/outlines. The repair recovered 22
previously unmirrored recordings without changing local-note hashes. All 291
currently returned note bodies were checked: 290 matched normalized original text
exactly; one retained its text with an unavailable, expired image explicitly
labelled. Prior mirrors absent from a later reply remain preserved. Chromium and
WebKit desktop/mobile checks covered named tabs, long text, tables/lists,
import loading/failure/retry and image limitations; the observed WebKit selector
width overflow was corrected and remeasured at the viewport width. Private source
payloads and screenshots are kept outside the repository.

### Chapter outline continuation (2026-10-02)

- [x] Add independent, full-canonical timestamped chapter generation, immutable
  provenance/revisions, explicit model-free time slices, profile/cost guardrails,
  outline-only durable retries and edit staleness; see `docs/chapter-outline.md`.
- [x] Add isolated regression coverage for schema upgrade, corrected transcript
  inputs, full tail coverage, legal playback bounds, no-egress, failure isolation,
  crash recovery and API queue feedback.
- [ ] Validate topic quality with an explicitly authorized real-model recording;
  automatic chapter generation remains opt-in. Remote-worker outline dispatch is
  implemented with capability checks, checksums, idempotency and isolated failure
  recovery; real-host validation and deployment remain unperformed.
