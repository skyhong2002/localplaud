# localplaud — status & TODO

Working notes for continuing development (synced across machines via git).
No secrets here — those live in `.env` / the Caddyfile, never committed.

## Status snapshot (2026-07-10)

- Full app built & published: <https://github.com/skyhong2002/localplaud> (MIT).
  Active development is merged directly to `main` (120 tests passing locally).
- **Production is LIVE on SkyLabMac** (M4 Mac mini): launchd service `com.localplaud.agent` runs `localplaud run`; reverse-proxied by the existing Caddy at **https://plaud.observe.tw** (basic_auth). Local ASR = mlx-whisper (Metal); LLM/embeddings = ollama.
- **Real account verified**: the official Open API provider is live in production
  (OAuth auto-refresh verified) and returns the account's **full history (~750
  recordings)**. Raw audio download works without requesting Plaud AI generation.
- **Product direction changed**: localplaud must replace the Plaud Intelligence
  subscription workflow. Plaud is retained only for recorder → App → raw-audio
  cloud transport. See `AGENTS.md` and `docs/product-workflow.md`.
- **Current production is not yet subscription-independent**: its running process
  started with `prefer_cloud_artifacts = true`, previously imported Plaud artifacts,
  and diarization disabled. The earlier embedding blocker is repaired on the host
  (`bge-m3` installed and `/api/embed` smoke-tested), but the service still needs a
  controlled restart onto the new independent-mode code and backlog verification.
- Dev env on SkyLabMac: `~/Projects/localplaud` (venv, ffmpeg static, config.toml, `.env`). Claude Code CLI installed (`~/.local/bin/claude`).

## TODO — prioritized

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
  and Resume retries only missing/failed work while Rebuild all is explicit.
- ✅ Fixed Ollama embeddings: model-level health checks now distinguish a healthy
  daemon from a missing configured model, errors give the exact `ollama pull` action,
  modern `/api/embed` batches inputs with legacy compatibility, and stored provenance
  includes the model. Pulled `bge-m3` on SkyLabMac and smoke-tested two 1024-d vectors.
- ✅ Queue is newest-first, and daemon work is bounded by configurable
  `pipeline.files_per_cycle` so fresh recordings can enter between backlog batches;
  `localplaud work --once` remains the explicit full-backlog path. Stage/status
  counts provide progress; automatic retry backoff policy remains to be added.

### P0 — SOTA speech and speakers

- ✅ Defaulted Apple Silicon to `mlx-community/whisper-large-v3-turbo` and pinned
  NumPy below 2.5 for mlx-whisper/numba compatibility. SkyLabMac downloaded the
  1.61 GB model and completed a local Metal smoke test with word timestamps.
  CUDA/CPU still needs the equivalent turbo deployment verified on its target host.
- Add VAD and word-level alignment, then production-quality pyannote diarization and
  assign speakers to words/segments. Whisper itself is not speaker-aware.
- Persist stable speaker IDs separately from editable display names.
- Add a custom vocabulary/correction layer for names, specialist terms, Taiwan
  Mandarin, and Mandarin/English code-switching.
- Establish a benchmark set from consented user-owned recordings: WER/CER, diarization
  error, timestamp quality, hallucination rate, runtime, and memory.

### P0 — Full-transcript notes and usable knowledge

- Replace the current 24,000-character truncation with full-coverage hierarchical
  summarization.
- Make templates editable data; support auto selection, per-file custom generation,
  multiple note tabs, provenance, and safe regeneration.
- Re-index the corrected canonical transcript. Add single-file Ask and whole-library
  Ask with playable timestamp citations and save-to-note.

### P1 — Plaud-like Web App workflow

- Implement `docs/product-workflow.md`: library filters/folders/tags, responsive
  split panes, persistent player, waveform/progress, transcript editing, speaker
  naming, notes, mind map, Ask, processing UI, and actionable recovery.
- Match the audited daily navigation model: Home/recent files, Search, all files,
  uncategorized, trash/recovery, folders, capture-source facets, library Ask,
  Templates, Discover/Automation, and Settings with responsive persistence.
- Add sortable library columns (name, duration, creation date), visible processing
  state, attention indicators, bulk selection, and safe recovery from trash.
- Add an explicit raw-ASR versus corrected-canonical transcript switch, synchronized
  timestamps/speaker labels, transcript-local search and find/replace, and preserve
  edits independently from the raw artifact.
- Add file Ask suggested questions and reusable local skills (action items, task
  table, insights), plus grounded follow-ups and save-to-note.
- Build template My Space and Explore surfaces with search, categories/scenarios,
  first-party/community provenance, authorship, descriptions, and versioned install
  or copy-to-workspace behavior.
- Consolidate copy/export actions in the file workspace. At minimum support the
  audited transcript choices TXT/SRT/DOCX/PDF with timestamp and speaker-label
  toggles, then retain the broader localplaud export targets below.
- Treat the Web App as the product, not a status viewer. CLI remains setup/ops tooling.
- Add original localplaud visual design with Plaud-like interaction density and
  information architecture; do not copy Plaud assets.
- Export audio, TXT/Markdown/SRT/VTT/DOCX/PDF transcripts, Markdown/DOCX/PDF notes,
  and PNG/Markdown mind maps with speaker/timestamp options.

### P2 — Automation and integrations

- Rules matching source, duration, early-transcript keyword, folder/tag, and metadata.
- Per-rule ASR/diarization/template selection, notification, email, webhook, and
  export actions with independent retry/history.
- Add a Discover hub for AutoFlow, local applications, and integrations. AutoFlow
  must show enablement, notification state, a readable trigger/action sentence,
  ownership/editability, history, and failures; local rules must be editable on Web,
  while externally owned rules are clearly read-only.
- Add settings sections for account/security and active sessions, workspace
  personalization, locale/preferences, vocabulary, private sync/backup, authorized
  apps/integrations, support, and version/about. Show integration scopes, health,
  last use, and revoke controls without mixing them with destructive account actions.
- Native PKCE inside localplaud to remove the Node.js dependency from first login.

### P1 — Deploy the other two machines
- **CCLabPC** (nvplaud.observe.tw, NVIDIA/CUDA): docker `gpu` profile or native; needs user in `docker` group. DNS already points here.
- **Oracle** (plaud.skyhong.tw, aarch64 CPU): `cpu` slim image (already builds/runs there) + Caddy vhost; cloud ASR.
- Pattern to reuse: append a `<domain> { basic_auth … ; reverse_proxy 127.0.0.1:8080 }` block to that host's Caddyfile (SkyLabMac already done this way).

### Housekeeping
- Optional: root LaunchDaemon so production starts on boot without login (needs sudo).

## Ops quick-reference (SkyLabMac)
- Update prod: `git -C ~/Projects/localplaud pull && launchctl kickstart -k gui/$(id -u)/com.localplaud.agent`
- Logs: `~/Projects/localplaud/data/service.{out,err}.log`
- Service: `launchctl list | grep localplaud`; plist at `~/Library/LaunchAgents/com.localplaud.agent.plist`
- Caddy vhost: block for `plaud.observe.tw` in `/usr/local/etc/caddy/Caddyfile` (basic_auth user `sky`); reload `caddy reload --config /usr/local/etc/caddy/Caddyfile`
- Session/creds: `~/Projects/localplaud/.env` (git-ignored)
