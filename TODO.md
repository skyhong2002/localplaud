# localplaud — status & TODO

Working notes for continuing development (synced across machines via git).
No secrets here — those live in `.env` / the Caddyfile, never committed.

## Status snapshot (2026-07-10)

- Full app built & published: <https://github.com/skyhong2002/localplaud> (MIT).
  The current local worktree has 333 tests passing; the latest P0 hardening is
  not yet committed or pushed.
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
  after fixing thinking-only Ollama completions. Pyannote Community-1 access is now
  authenticated and a 192-second real recording completed with two stable speakers,
  107 assigned segments, and 445 assigned words. **Diarization backfill complete
  (2026-07-11):** all 25 previously degraded/interrupted recordings resumed
  successfully overnight — 33 recordings now have completed diarize stages and
  zero degraded/failed remain.
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
`plaud.provider = "official"` (default): OAuth via the official Plaud CLI
(`localplaud auth login` wraps it; tokens in `~/.plaud/tokens.json`,
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
  and Resume retries only missing/failed work while Rebuild all is explicit. Raw ASR,
  aligned, and diarized outputs are retained as separate artifacts; an atomic durable
  processing lease rejects duplicate Web/daemon/multi-process claims.
- ✅ Fixed Ollama embeddings: model-level health checks now distinguish a healthy
  daemon from a missing configured model, errors give the exact `ollama pull` action,
  modern `/api/embed` batches inputs with legacy compatibility, and stored provenance
  includes the provider endpoint/model embedding-space identity, so equal-width
  vectors from incompatible models are never mixed. Pulled `bge-m3` on SkyLabMac and
  smoke-tested two 1024-d vectors.
- ✅ Queue is newest-first, and daemon work is bounded by configurable
  `pipeline.files_per_cycle` so fresh recordings can enter between backlog batches;
  `localplaud work --once` remains the explicit full-backlog path. Stage/status
  counts provide progress. ✅ Added bounded exponential retry scheduling with
  durable retry count/deadline; terminal failures stop retrying at the configured
  ceiling.

### P0 PRIMARY — Provider, model, and execution profiles

**Outcome:** every derived stage can independently use a local model, a cloud API,
or a remote worker. localplaud resolves those choices into a reusable execution
profile, records the resolved snapshot on each run, and never crosses a privacy or
cost boundary through an implicit fallback.

Backend implementation status:

- ✅ Provider registries/config already exist for ASR, LLMs, and embeddings;
  OpenAI-compatible `base_url` configuration exists for relevant API paths.
- ✅ Stage runs and artifacts already retain provider/model provenance, and health
  checks can distinguish some daemon-level and model-level failures.
- ✅ Added shared capability contracts, durable provider/model/profile/health
  records, deterministic layered resolution, masked secret references, explicit
  no-egress/allowed-provider/quality/timeout/cost/fallback checks (including every
  fallback), immutable `StageAttempt`
  snapshots, and headless management/observability APIs.
- ✅ Added truthful Apple/NVIDIA/CPU, OpenAI Cloud, partial OpenAI-compatible, and
  Remote GPU starting profiles plus conservative hardware detection; profiles are
  recommendations and are never silently selected. Placeholder compatible/worker
  endpoints must be explicitly configured and health-checked.
- ✅ Added separately declared OpenAI-compatible audio/text/embedding capabilities,
  custom base URL/headers/timeout/input limits, and disabled-by-default experimental
  `codex-local` through an ephemeral read-only Codex CLI boundary. OpenAI Audio
  dispatch now follows model-specific response shapes: `whisper-1` verbose word
  timestamps, GPT-4o transcription JSON, and diarized speaker segments, without
  inventing unsupported timestamp precision.
- ✅ Added remote-worker protocol v1 and reference durable server: HMAC handshake,
  idempotent jobs, signed file-scoped fetch, progress/cancellation, structured
  retryable errors, expiring claim leases/heartbeats, crashed-runner recovery,
  stale-owner rejection, checksum validation, and a remote ASR adapter with no
  Plaud credentials.
- Remaining external acceptance: NVIDIA/CPU host execution, a separately isolated
  rentable GPU, and official OpenAI cloud execution. OpenCode Go is verified as a
  deliberately text-only compatible connection; it does not satisfy audio or
  embedding evidence.
- ✅ **Rentable GPU trial PASSED (2026-07-10, gputw.ai):** full API-driven lifecycle
  (catalog → node → create → RUNNING in ~20 s → stop), RTX 3090 x2 NVLink pod at
  $0.4681/hr per-second billing. Reference worker deployed with zero Plaud
  credentials; HMAC handshake over the public `8080-<instance-id>.gputw.ai` HTTPS
  proxy; signed `/api/worker-input/` audio fetch through a Caddy basic_auth bypass;
  two real Mandarin recordings transcribed end-to-end (196 s audio → 10.8 s wall,
  RTF ≈ 0.06 with cached large-v3-turbo on CUDA). Caveats: SSH public keys are
  dashboard-only (no API); one single-3090 node was stuck "releasing the previous
  instance"; creating an instance rents the whole node (2 GPUs / 24 cores), so
  costs above the headline per-GPU price. Instance `267f6c1d…` is kept STOPPED for
  reuse; worker deploy = rsync/tar + `uv` + `worker_main.py` (see acceptance
  matrix). gputw.ai stays in the dev flow as the rentable-GPU path.
- **Benchmark decision (2026-07-10):** Sky explicitly deprioritized the consented
  Taiwan Mandarin / Mandarin-English benchmark acceptance — the core pipeline (ASR,
  diarization, summary, mind map, Ask) runs without it. The `benchmark-speech`
  harness stays available, but benchmark runs no longer block P0 completion; they
  remain a prerequisite only for changing defaults that need accuracy evidence
  (e.g. enabling VAD by default or adopting forced alignment).

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
  assignment, model provenance, and actionable health checks. Model terms/token and
  a real SkyLabMac smoke are verified: a 192-second recording completed in 99.44
  seconds with two stable speakers, 107 assigned segments, and 445 assigned words.
  VAD and forced-alignment quality still require the private benchmark set.
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
  user recordings first. A real `align` stage now validates Whisper word timestamps
  and optionally dispatches WhisperX; missing alignment is visibly degraded.
- ✅ Persist stable speaker IDs separately from editable display names: `speakers`
  rows mirror the diarization keys per recording, renames are upserted from the
  Web detail page (legend inline forms), and flow into transcript view, regenerated
  notes/indexes, Ask, and Markdown export. A rename invalidates stale derived
  artifacts and re-indexes without ASR. ✅ Added conservative time-overlap identity
  reconciliation plus a durable speaker timeline across intermediate speaker-less ASR.
- ✅ Added durable custom vocabulary/correction data and an independently resumable
  correction stage. Automatic corrections create provenance-preserving revisions
  and never overwrite a later user revision.
- ✅ Added a privacy-safe `benchmark-speech` harness for consented user-owned data:
  WER/CER, DER proxy, timestamp MAE, insertion/hallucination proxy, runtime/RTF, and
  peak memory with fail-closed thresholds. The private benchmark set and real runs
  remain external acceptance work and are never committed.

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
  included in Markdown export. ✅ Added a full-outline local PNG renderer/download
  with hierarchical connectors, long-node wrapping, and CJK-capable font fallback.
- ✅ Templates are editable/versioned database records with auto selection,
  per-file generation, multiple template tabs, profile/transcript provenance, and
  safe regeneration history.
- ✅ Single-file Ask: `/file/{id}/ask` answers grounded only in one recording, with
  citations rendered as playable timestamp buttons that seek the player; suggested
  grounded question chips; graceful degrade when unindexed or providers are down.
- ✅ Whole-library Ask citations now deep-link to `/file/{id}?t={start}` and seek the
  player on load, so a cited answer opens the recording at the cited moment.
- ✅ Added durable single-file/library Ask threads with grounded follow-up history,
  provider/profile provenance, and save-to-local-note with playable source links.
- ✅ Transcript corrections as revisions: inline per-segment editing on the Web
  detail page creates immutable `transcript_revisions` on top of the untouched raw
  ASR row; the latest revision is the canonical transcript for summaries, indexing,
  and export, edits survive re-ASR, and each edit hides/invalidates stale notes and
  maps while rebuilding the embedding index in the background without rerunning ASR
  (notes/map regeneration stays explicit through Resume). Provenance prevents edits
  of Plaud imports from satisfying independent mode. ✅ Added transcript-wide literal
  find/replace, conflict-checked multi-segment bulk edits, a visible/API revision
  history, and transcript-revision links on notes, maps, indexed chunks, and Ask
  citations.

### P1 — Plaud-like Web App workflow

- Implement `docs/product-workflow.md`: library filters/folders/tags, responsive
  split panes, persistent player, waveform/progress, transcript editing, speaker
  naming, notes, mind map, Ask, processing UI, and actionable recovery.
- Match the audited daily navigation model: Home/recent files, Search, all files,
  uncategorized, trash/recovery, folders, capture-source facets, library Ask,
  Templates, Discover/Automation, and Settings with responsive persistence.
- ✅ Added sortable library columns (name, duration, recorded date) with direction
  indicators, processing-state and capture-source filters, an always-visible per-row
  processing state, error/partial attention indicators, and a read-only trash mirror
  view with count (localplaud never deletes cloud data). `/` and `/api/files` share
  the sort/state/scene/view params and fall back safely on bad input. Bulk selection,
  folders/tags, and uncategorized organization still remain.
- ✅ (partial) Explicit raw-ASR versus corrected-canonical transcript switch with
  synchronized timestamps/speaker labels is live, and edits are preserved
  independently from the raw artifact. Transcript-wide find/replace and bulk-edit
  APIs are live. Remaining P1 polish: client-side transcript-local navigation/search.
- Add file Ask suggested questions and reusable local skills (action items, task
  table, insights), plus grounded follow-ups and save-to-note.
- Build template My Space and Explore surfaces with search, categories/scenarios,
  first-party/community provenance, authorship, descriptions, and versioned install
  or copy-to-workspace behavior.
- Consolidate copy/export actions in the file workspace. At minimum support the
  audited transcript choices TXT/SRT/DOCX/PDF with timestamp and speaker-label
  toggles, then retain the broader localplaud export targets below.
- Treat the Web App as the product, not a status viewer. CLI remains setup/ops tooling.
- Add provider/model/profile management to Settings: connection setup, capability
  and model health, recommended local profiles, cost/privacy policy, remote workers,
  resolution preview, defaults, and a per-recording override/reprocess picker. Keep
  secrets masked and make the actual selected provider visible during processing.
- Add original localplaud visual design with Plaud-like interaction density and
  information architecture; do not copy Plaud assets.
- Export audio, TXT/Markdown/SRT/VTT/DOCX/PDF transcripts, Markdown/DOCX/PDF notes,
  and PNG mind maps with speaker/timestamp options (Markdown mind-map export ships
  in the combined Markdown export).

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

- Rules matching source, duration, early-transcript keyword, folder/tag, and metadata.
- Per-rule named execution-profile and template selection, notification, email,
  webhook, and export actions with independent retry/history. Rules may override
  safe profile fields, but must not duplicate credentials or silently cross the
  profile's privacy/cost policy.
- Add a Discover hub for AutoFlow, local applications, and integrations. AutoFlow
  must show enablement, notification state, a readable trigger/action sentence,
  ownership/editability, history, and failures; local rules must be editable on Web,
  while externally owned rules are clearly read-only.
- Add settings sections for account/security and active sessions, workspace
  personalization, locale/preferences, vocabulary, private sync/backup, authorized
  apps/integrations, support, and version/about. Show integration scopes, health,
  last use, and revoke controls without mixing them with destructive account actions.
- Native PKCE inside localplaud to remove the Node.js dependency from first login.

### Housekeeping
- Optional: root LaunchDaemon so production starts on boot without login (needs sudo).

## Ops quick-reference (SkyLabMac)
- Update prod: `git -C ~/Projects/localplaud pull && launchctl kickstart -k gui/$(id -u)/com.localplaud.agent`
- Logs: `~/Projects/localplaud/data/service.{out,err}.log`
- Service: `launchctl list | grep localplaud`; plist at `~/Library/LaunchAgents/com.localplaud.agent.plist`
- Caddy vhost: block for `plaud.observe.tw` in `/usr/local/etc/caddy/Caddyfile` (basic_auth user `sky`); reload `caddy reload --config /usr/local/etc/caddy/Caddyfile`
- Session/creds: `~/Projects/localplaud/.env` (git-ignored)
