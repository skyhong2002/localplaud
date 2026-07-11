# localplaud — status & TODO

Working notes for continuing development (synced across machines via git).
No secrets here — those live in `.env` / the Caddyfile, never committed.

## Status snapshot (2026-07-11)

- Full app built & published: <https://github.com/skyhong2002/localplaud> (MIT).
  Active development is merged directly to `main` (284 tests passing locally).
- **Production is LIVE on SkyLabMac** (M4 Mac mini): launchd service `com.localplaud.agent` runs `localplaud run`; reverse-proxied by the existing Caddy at **https://plaud.observe.tw** (basic_auth). Local ASR = mlx-whisper (Metal); LLM/embeddings = ollama.
- **Real account verified**: the official Open API provider is live in production
  (OAuth auto-refresh verified) and returns the account's **full history (~750
  recordings)**. Raw audio download works without requesting Plaud AI generation.
- **Product direction changed**: localplaud must replace the Plaud Intelligence
  subscription workflow. Plaud is retained only for recorder → App → raw-audio
  cloud transport. See `AGENTS.md` and `docs/product-workflow.md`.
- **Independent mode is live in production**: the service was backed up and
  restarted on the current `main` with `artifact_mode = "independent"` and
  `prefer_cloud_artifacts = false`; local transcript revisions preserve provenance,
  mind maps are resumable, and a real failed mind-map stage was retried successfully
  after fixing thinking-only Ollama completions. The remaining production quality
  blocker is diarization: pyannote is configured, but model-term acceptance and a
  Hugging Face token are still missing, so affected files remain usable `partial`.
- Dev env on SkyLabMac: `~/Projects/localplaud` (venv, ffmpeg static, config.toml, `.env`). Claude Code CLI installed (`~/.local/bin/claude`).

## TODO — prioritized

Priority map:

- **P0:** production-safe independent processing; the primary provider/model/profile
  platform; production speech/diarization; full-coverage notes, mind maps, and Ask.
- **P1:** expose the P0 capabilities as a complete daily-use Web App, then validate
  the same product and execution profiles on Apple, NVIDIA, and CPU hosts.
- **P2:** build AutoFlow and integrations on named P0 profiles and P1 controls; rules
  orchestrate proven capabilities instead of inventing a second provider system.

### ✅ DONE (2026-07-10) — Foundation: official Open API and raw audio
`plaud.provider = "official"` (default): native S256 PKCE OAuth through
`localplaud auth login`; tokens remain official-CLI-compatible in `~/.plaud/tokens.json`,
auto-refresh implemented in `plaud/oauth.py`, verified live — both tokens
rotate, 24h expiry). `/open/third-party/files/{id}` supplies a signed raw-audio
URL. The client can also import Plaud transcripts/summaries, but that capability is
now migration/debug-only and cannot be a primary pipeline dependency. api-apse1 is
optional enrichment (`plaud.apse1_enrichment`, needs a pasted session) for
`version`/`file_md5`/`edit_time`/`is_trash`. Full API notes: `docs/plaud-api.md`.

### P0 — Make raw-audio processing production-safe

- ✅ Added default `pipeline.artifact_mode = "independent"`: only `source=local`
  transcripts satisfy the pipeline. Explicit `migration` mode retains the old
  comparison/backfill behavior; automatic cloud import requires both migration mode
  and `prefer_cloud_artifacts = true`.
- ✅ Changed transcript storage to preserve multiple provenance rows. The canonical
  Web/API/CLI/export surface prefers local output while labelling Plaud-only imports;
  independent export excludes paid Plaud artifacts.
- ✅ Added an idempotent legacy-data preparation: preserve Plaud transcript/notes,
  relabel local summaries derived from a cloud-only transcript as `legacy`, clear
  their non-provenanced chunks, and requeue audio for local ASR. Available explicitly
  as `localplaud prepare-independent` and run once on independent-mode startup.
- ✅ Added durable per-recording stage runs with attempt count, status, provider/model,
  artifact source, timestamps, and errors. ASR is persisted before optional work;
  diarization, notes, and indexing failures produce an actionable `partial` state
  without discarding usable artifacts. Web detail/status pages expose diagnostics,
  and Resume retries only missing/failed work while Rebuild all is explicit.
- ✅ Fixed Ollama embeddings: model-level health checks now distinguish a healthy
  daemon from a missing configured model, errors give the exact `ollama pull` action,
  modern `/api/embed` batches inputs with legacy compatibility, and stored provenance
  includes the model. Pulled `bge-m3` on SkyLabMac and smoke-tested two 1024-d vectors.
- ✅ Queue is newest-first, and daemon work is bounded by configurable
  `pipeline.files_per_cycle` so fresh recordings can enter between backlog batches;
  `localplaud work --once` remains the explicit full-backlog path. Stage/status
  counts provide progress. Failed and usable-partial cycles now retry automatically
  with durable bounded exponential backoff; fresh downloads stay ahead of due
  retries, exhaustion is visible, and manual Resume immediately resets the budget.

### P0 PRIMARY — Provider, model, and execution profiles

**Outcome:** every derived stage can independently use a local model, a cloud API,
or a remote worker. localplaud resolves those choices into a reusable execution
profile, records the resolved snapshot on each run, and never crosses a privacy or
cost boundary through an implicit fallback.

Backend foundation landed on 2026-07-11, but this is not yet the finished feature:

- ✅ Provider registries/config already exist for ASR, LLMs, and embeddings;
  OpenAI-compatible `base_url` configuration exists for relevant API paths.
- ✅ Stage runs and artifacts already retain provider/model provenance, and health
  checks can distinguish some daemon-level and model-level failures.
- ✅ Added provider-neutral capability contracts for transcription, alignment,
  diarization, correction, notes, mind maps, embeddings, and Ask. Durable database
  records now cover provider connections, model catalog entries, versioned execution
  profiles, per-stage selections, and per-recording overrides; ordinary rows retain
  only opaque secret references.
- ✅ Added deterministic layered resolution (system → folder/rule → template →
  recording), immutable JSON snapshots, capability validation, no-egress enforcement,
  an idempotent Settings-equivalent bootstrap, legacy SQLite migration, and headless
  list/preview/override APIs.
- ✅ Pipeline stages now dispatch through the resolved recording profile without
  mutating process-wide Settings. The immutable snapshot is persisted on stage runs,
  transcripts, notes, and embedding chunks; local-only profiles disable legacy cloud
  fallback. Recordings expose a profile picker for the next Resume/Rebuild.
- ✅ Added provider/model/profile CRUD APIs, connection configuration health, and a
  Settings surface for inspecting connections/profiles, testing health, and creating
  secret-reference-only connections. Raw credentials are rejected by the API.
- ✅ Connection and model health actions now execute the real provider/runtime health
  implementation (including configured model checks) and persist checked status,
  detail, and timestamp. Secret references resolve only from explicit `env:` names.
- ✅ Settings can add model capabilities and construct an explicit per-stage profile;
  the API now provides guarded create/update/delete operations for connections,
  models, and immutable profile versions.
- ✅ Existing connections and models can be edited or safely deleted from Settings;
  immutable profiles expose a guided “New version” flow and guarded deletion for
  non-default, unused versions.
- ✅ Added truthful local hardware/runtime detection and ranked Apple Silicon MLX,
  NVIDIA CUDA faster-whisper, and CPU fallback recommendations. Settings reports
  architecture, memory, GPU/runtime evidence and missing requirements; only verified
  ready recommendations can create an idempotent profile. Installation replaces only
  ASR/alignment while preserving every other stage and privacy/cost/fallback policy.
- ✅ Added an append-only stage-attempt usage ledger. Every real attempt retains its
  resolved profile, selected/actual provider and model, status, latency, normalized
  audio/text/token/request usage, errors, and catalog-priced estimated USD cost.
  Recording details expose attempt history and totals; Status aggregates execution
  hours/cost, and model setup accepts explicit token/audio price metadata. Missing
  rates honestly produce zero rather than invented prices.
- ✅ Profile cost ceilings now enforce a pre-egress reservation boundary. Cloud and
  remote stages with a ceiling require explicit catalog pricing or an explicit free
  declaration; conservative audio/text/output projections are checked against all
  prior attempt cost before the provider call. Rejections make zero provider calls,
  remain traceable failures, and can Resume after selecting a new policy/profile.
- ✅ Added authenticated `localplaud-worker` protocol v1: versioned capability
  handshake, durable/idempotent jobs, progress, cancellation, structured retryable
  errors, minimum typed inputs, restart recovery, and SHA-256 artifact verification.
  Credential-shaped Plaud/provider fields are rejected recursively and bearer tokens
  remain environment-only. Pipeline dispatch covers transcribe, diarize, notes, mind
  maps, and embeddings.
- ✅ CCLabPC NVIDIA acceptance: the CUDA image now pins a compatible PyTorch 2.8 /
  CUDA 12.8 / TorchCodec 0.7 / pyannote 4 stack. The image imports cleanly, sees the
  RTX 5060 through NVIDIA Container Toolkit, and completed an authenticated v1
  capability handshake without interrupting the existing processing container.
- Remaining: validate one rentable GPU host, then add explicit cross-provider
  fallback and benchmark acceptance.

Implement this P0 in the following order:

1. **Stage and capability contracts.** Define a common provider interface for ASR,
   alignment, diarization, transcript correction, notes, mind maps, embeddings, and
   Ask. Capabilities must declare supported languages, timestamps/word timestamps,
   speaker output, streaming/batch behavior, prompt limits, input limits, required
   hardware, data-egress behavior, and health state. Treat OpenAI-compatible text,
   audio, and embeddings as three separately declared capabilities; supporting one
   must not imply the other two.
2. **Durable provider/model/profile schema.** Store provider connections, model
   catalog entries, secret references, reusable execution profiles, health checks,
   and versions in the local database. A profile selects a provider/model and
   stage-specific options for every enabled stage. Never store API keys directly in
   ordinary profile or artifact rows.
3. **Deterministic profile resolution.** Resolve in this order: system default →
   folder/AutoFlow rule → template default → per-recording override. Persist the
   fully resolved profile snapshot on every `StageRun` and derived artifact so later
   settings changes do not rewrite history. Reprocessing may explicitly select a
   newer profile or preserve the previous one.
4. **Local hardware profiles.** Ship truthful starting profiles for Apple Silicon
   (MLX Whisper large-v3-turbo), NVIDIA/CUDA (faster-whisper or verified WhisperX
   integration plus pyannote), and CPU/other GPU (whisper.cpp or faster-whisper where
   supported). Detect available hardware, memory, runtimes, and installed models,
   then recommend rather than silently force a profile. Do not claim acceleration
   on an unverified backend.
5. **Cloud and compatible API profiles.** Support OpenAI Audio, text/Responses or
   chat-compatible generation, and embeddings as explicit capabilities; preserve
   the existing Deepgram and AssemblyAI ASR paths; and support custom
   OpenAI-compatible base URL, key reference, model name, headers, timeout, and
   limits per capability. Add an experimental trusted-single-user `codex-local`
   text provider only through a supported Codex CLI/app-server boundary: never copy
   or scrape ChatGPT/Codex auth tokens, never present it as a generic
   OpenAI-compatible endpoint, and never enable it by default on a public or
   multi-user deployment.
6. **Remote GPU worker.** Define a versioned `localplaud-worker` protocol with
   capability handshake, authenticated job submission, input transfer or signed
   fetch, progress, cancellation, retry/idempotency, checksummed artifacts, and
   structured errors. Workers receive only the minimum audio/job data and never
   Plaud OAuth credentials. Validate both a self-owned NVIDIA host and one rentable
   GPU deployment path.
7. **Policy, fallback, and observability.** Profiles declare local-only/no-egress,
   allowed providers, retry/timeout policy, fallback order, quality floor, and
   optional cost ceiling. Never silently fall back from local to external. Show the
   selected and actual provider/model, degraded capability, queue target, latency,
   audio seconds/tokens, and estimated/actual cost where providers expose enough
   data.
8. **API and acceptance matrix.** Add APIs for connections, models, capabilities,
   profiles, resolution previews, health tests, and per-recording overrides. Migrate
   the current config into an equivalent default profile without changing existing
   behavior. Test clean raw-audio completion on Apple local, NVIDIA local, CPU or
   other supported fallback, OpenAI cloud, one partial OpenAI-compatible service,
   and one remote worker. Benchmark Taiwan Mandarin and Mandarin/English recordings
   before changing production defaults.

The backend contracts, persistence, resolver, policy enforcement, and headless APIs
are P0. The complete Settings/profile editor and per-recording picker are the P1 Web
surface for this P0 foundation; AutoFlow consumes named profiles in P2 instead of
embedding raw provider credentials or model settings in each rule.

### P0 — SOTA speech and speakers

- ✅ Defaulted Apple Silicon to `mlx-community/whisper-large-v3-turbo` and pinned
  NumPy below 2.5 for mlx-whisper/numba compatibility. SkyLabMac downloaded the
  1.61 GB model and completed a local Metal smoke test with word timestamps.
  CUDA/CPU still needs the equivalent turbo deployment verified on its target host.
- ✅ Updated the diarization integration from legacy pyannote 3.1 to the current
  open-source `speaker-diarization-community-1` API, including word/segment speaker
  assignment, model provenance, and actionable health checks. SkyLabMac still needs
  the optional dependency plus acceptance of the gated model terms and a Hugging
  Face token before real-audio verification. VAD and word-level alignment remain.
- ✅ Added optional VAD groundwork behind a **default-off** `asr.vad.enabled` flag
  (`asr/vad.py`): provider-agnostic silero-vad detection + region merge/pad/split
  planning, ffmpeg region slicing, and honest `health()`. The mlx path transcribes
  merged speech regions and offsets timestamps back to global time; the
  faster-whisper path wires its native bundled-silero `vad_filter`. Missing the
  optional `vad` extra is a *degraded* (not failed) state: ASR logs a warning,
  falls back to whole-file transcription, and the provider `health()` says so.
  Remaining: benchmark VAD on real Taiwan Mandarin / code-switch recordings before
  enabling it by default. **Word-level forced alignment is deliberately NOT
  implemented here** — Whisper's own word timestamps are currently the alignment
  source, and a whisperX-style wav2vec2 forced aligner needs per-language models
  plus a Mandarin/code-switch accuracy evaluation, so it must be benchmarked on real
  user recordings first.
- ✅ Persist stable speaker IDs separately from editable display names: `speakers`
  rows mirror the diarization keys per recording, renames are upserted from the
  Web detail page (legend inline forms), and flow into transcript view, regenerated
  notes/indexes, Ask, and Markdown export. A rename invalidates stale derived
  artifacts and re-indexes without ASR. Diarization reruns now reconcile run-local
  labels against the previous speech timeline with one-to-one overlap matching;
  ambiguous/new voices receive a fresh unnamed identity rather than inheriting a
  user's display name. The mapping is retained in stage-attempt provenance.
- ✅ Added a durable custom vocabulary/correction layer for names, specialist terms,
  Taiwan Mandarin, and Mandarin/English code-switching. Rules support language and
  case scope, longest non-overlapping matching, Settings CRUD, and explicit library
  application. New local ASR applies rules automatically as an immutable revision;
  raw provider output stays untouched and dependent artifacts become visibly stale.
- Establish a benchmark set from consented user-owned recordings: WER/CER, diarization
  error, timestamp quality, hallucination rate, runtime, and memory.

### P0 — Full-transcript notes and usable knowledge

- ✅ Replaced the 24,000-character truncation with bounded hierarchical
  map/reduce summarization. Every transcript chunk contributes coverage notes before
  the selected template produces final Markdown; stage provenance records strategy,
  transcript size, chunks, and map/reduce call counts.
- ✅ Added a `mind_map` pipeline stage (toggle `pipeline.mind_map`, default on):
  a full-coverage nested Markdown outline built from the canonical transcript with
  the same bounded map/reduce chunking (existing local notes are structural context
  only). Stored as a provenanced `mind_map` note, resumable/degradable like other
  optional stages, rendered as a collapsible tree tab in the Web detail page, and
  included in Markdown export. PNG mind-map export remains.
- ✅ Note templates are editable, versioned database records seeded from the built-in
  catalog. Settings can create templates or immutable new versions; recordings select
  a template independently, changes mark notes/maps stale for explicit Resume, remote
  workers receive the exact prompt snapshot, and generated notes/export retain the
  template version and full prompt provenance. Multiple template notes remain visible
  as tabs; local deterministic automatic template selection is also implemented.
- ✅ Single-file Ask: `/file/{id}/ask` answers grounded only in one recording, with
  citations rendered as playable timestamp buttons that seek the player; suggested
  grounded question chips; graceful degrade when unindexed or providers are down.
- ✅ Whole-library Ask citations now deep-link to `/file/{id}?t={start}` and seek the
  player on load, so a cited answer opens the recording at the cited moment.
- ✅ Ask conversations are durable grounded threads for both one recording and the
  whole library. Follow-ups retain bounded conversation context while retrieval and
  citations remain grounded in the current query. Any assistant answer can be saved
  idempotently as an editable note with source moments; Saved notes has its own page,
  recording tabs, edit/delete controls, deep links, and Markdown export coverage.
- ✅ Transcript corrections as revisions: inline per-segment editing on the Web
  detail page creates immutable `transcript_revisions` on top of the untouched raw
  ASR row; the latest revision is the canonical transcript for summaries, indexing,
  and export, edits survive re-ASR, and each edit hides/invalidates stale notes and
  maps while rebuilding the embedding index in the background without rerunning ASR
  (notes/map regeneration stays explicit through Resume). Provenance prevents edits
  of Plaud imports from satisfying independent mode. Find/replace, bulk revisions,
  dependent-artifact lineage, revision history, and non-destructive restore are
  implemented.

### P1 — Plaud-like Web App workflow

- ✅ Vendored the pinned HTMX 1.9.12 runtime, upstream Zero-Clause BSD license,
  and SHA-256 manifest. The packaged Web App has no CDN dependency for daily
  interaction and remains functional on offline/private deployments.
- Implement `docs/product-workflow.md`: library filters/folders/tags, responsive
  split panes, persistent player, waveform/progress, transcript editing, speaker
  naming, notes, mind map, Ask, processing UI, and actionable recovery.
- Match the audited daily navigation model: Home/recent files, Search, all files,
  uncategorized, trash/recovery, folders, capture-source facets, library Ask,
  Templates, Discover/Automation, and Settings with responsive persistence.
- ✅ Added a dedicated Home dashboard separate from All files: recent recordings,
  operational library/audio/processing counts, metadata-only visibility, Plaud mirror
  progress, AutoFlow activity, attention queue, and direct Add/Import actions.
- ✅ Added sortable library columns (name, duration, recorded date) with direction
  indicators, processing-state and capture-source filters, an always-visible per-row
  processing state, error/partial attention indicators, and a read-only trash mirror
  view with count (localplaud never deletes cloud data). `/` and `/api/files` share
  the sort/state/scene/view params and fall back safely on bad input.
- ✅ Added local folders and tags with additive legacy-DB migration, guarded CRUD,
  counts and filters, a true uncategorized view, deterministic metadata in the JSON
  API, folder/tag pills on library and detail views, and atomic multi-recording bulk
  move/add/remove controls. The recording workspace now edits or clears folder/tags
  through the same atomic API. Organization never mutates Plaud cloud or trash state.
- ✅ Added a Plaud-style Add audio surface with local upload and a durable,
  background Import from Plaud job. It refreshes the full metadata catalog and any
  paid Plaud transcript/summary, never downloads audio during catalog import, and
  exposes a per-recording Import audio action for metadata-only rows. Scheduled
  polling now follows the same metadata-first default.
- ✅ (partial) Explicit raw-ASR versus corrected-canonical transcript switch with
  synchronized timestamps/speaker labels is live. Transcript-local search provides
  next/previous navigation, and case-aware replace-all creates one immutable bulk
  revision while preserving raw ASR and invalidating only dependent artifacts.
  Revision history exposes change reason/time, historical preview, current-state
  marking, and non-destructive restore-as-new-revision with stale-write protection.
  Summaries, mind maps, embedding chunks, and stage provenance now store and expose
  the exact raw transcript id/source plus revision they consumed.
- ✅ Replaced the native audio control with a responsive persistent player: locally
  generated/cached waveform, click/range seek, play/pause, −10/+30 seconds, playback
  speed, keyboard controls, deep-link seeking, and active transcript synchronization.
- ✅ Added explicit local-data lifecycle controls. Plaud-sourced audio/waveform can
  return to metadata-only for space recovery; local processing can be reset without
  deleting cloud artifacts, Saved notes, Ask history, organization, or Plaud data.
- ✅ Added durable local recording-title overrides with inline edit/revert. Plaud
  keeps its latest cloud title separately; local names survive sync and consistently
  drive library sort/search, detail, Ask/search citations, automation, CLI, and export.
- ✅ Search no longer depends on embeddings: local lexical results cover title,
  provenance-correct canonical transcript, generated notes, and Saved notes, with
  folder/tag/source/date filters and playable timestamp links. Available semantic
  hits are merged and deduplicated without weakening those filters.
- ✅ Added file Ask suggested questions and versioned, inspectable local quick actions
  for action items, task tables, and insights. They use the same recording-scoped
  retrieval, provider profile, citations, durable follow-ups, and save-to-note path;
  running one is read-only and never silently creates notes, tasks, or external work.
- ✅ Built dedicated Templates My Space and Explore surfaces with search,
  categories/scenarios, first-party/personal provenance, authorship, descriptions,
  popularity signals, prompt preview, immutable new-version editing, and
  copy-to-workspace behavior. Community/remote catalog ingestion remains optional.
- ✅ Added local deterministic Auto template selection with title/transcript/duration
  signals in English and Chinese, an explainable preview, confidence/reasons, and
  durable stage provenance for the actual selected template and engine version.
- ✅ Consolidated recording exports in one modal: canonical transcript TXT/SRT/VTT
  with timestamp and speaker-label toggles. Existing notes, archive, and original
  audio exports remain conveniences; no additional formats are required.
- Treat the Web App as the product, not a status viewer. CLI remains setup/ops tooling.
- Add provider/model/profile management to Settings: connection setup, capability
  and model health, recommended local profiles, cost/privacy policy, remote workers,
  resolution preview, defaults, and a per-recording override/reprocess picker. Keep
  secrets masked and make the actual selected provider visible during processing.
- Add original localplaud visual design with Plaud-like interaction density and
  information architecture; do not copy Plaud assets.
- Transcript TXT/SRT/VTT is the completed required export scope. Existing notes,
  original-audio, archive, and mind-map exports may remain as conveniences.

### P1 — Multi-host deployment

- **CCLabPC** (nvplaud.observe.tw, NVIDIA/CUDA): docker `gpu` profile or native;
  needs user in `docker` group. DNS already points here. Use this host to validate
  the NVIDIA Local execution profile and worker capability contract.
- **Oracle** (plaud.skyhong.tw, aarch64 CPU): `cpu` slim image (already builds/runs
  there) + Caddy vhost; use an explicit CPU or cloud profile rather than assuming GPU
  acceleration.
- Pattern to reuse: append a `<domain> { basic_auth … ; reverse_proxy
  127.0.0.1:8080 }` block to that host's Caddyfile (SkyLabMac already done this way).

### P2 — Automation and integrations

- ✅ Added executable local AutoFlow rules matching source, title keyword, duration,
  folder, and tag. Rules have priority, enable/disable, readable trigger/action
  sentences, mutation-free dry-run, versioned idempotency, metadata-sync hooks, and
  per-recording success/failure history with retry semantics.
- ✅ Rule actions can select a named execution profile and note template or move/add
  organization metadata; validation prevents dangling references.
- ✅ Notification-enabled rules now create a durable, deduplicated local inbox item
  after rule actions commit. Notifications support unread state, mark-all-read,
  dismissal, preserved rule/recording snapshots, and independent delivery retry;
  delivery failure never rolls back completed organization or processing actions.
  Remaining downstream actions: email, webhook, and export.
- ✅ Added a Discover hub for locally owned/editable AutoFlow rules, create/edit/
  delete controls, Run now, history, and notification policy, plus a responsive
  notification inbox with an unread badge. Remaining: local applications/integration
  catalog and externally owned read-only rules.
- Add settings sections for account/security and active sessions, workspace
  personalization, locale/preferences, vocabulary, private sync/backup, authorized
  apps/integrations, support, and version/about. Show integration scopes, health,
  last use, and revoke controls without mixing them with destructive account actions.
- ✅ Added native loopback S256 PKCE inside localplaud. First login no longer needs
  Node.js or the Plaud CLI; state, two-minute expiry, public-client exchange,
  atomic `0600` token storage, auto-refresh, CLI-compatible schema, and actionable
  port/denial/timeout errors are covered. Settings exposes non-secret auth status
  and the correct local login command without offering a misleading remote callback.

### Housekeeping
- Optional: root LaunchDaemon so production starts on boot without login (needs sudo).

## Ops quick-reference (SkyLabMac)
- Update prod: `git -C ~/Projects/localplaud pull && launchctl kickstart -k gui/$(id -u)/com.localplaud.agent`
- Logs: `~/Projects/localplaud/data/service.{out,err}.log`
- Service: `launchctl list | grep localplaud`; plist at `~/Library/LaunchAgents/com.localplaud.agent.plist`
- Caddy vhost: block for `plaud.observe.tw` in `/usr/local/etc/caddy/Caddyfile` (basic_auth user `sky`); reload `caddy reload --config /usr/local/etc/caddy/Caddyfile`
- Session/creds: `~/Projects/localplaud/.env` (git-ignored)
