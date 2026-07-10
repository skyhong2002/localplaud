# localplaud — status & TODO

Working notes for continuing development (synced across machines via git).
No secrets here — those live in `.env` / the Caddyfile, never committed.

## Status snapshot (2026-07-10)

- Full app built & published: <https://github.com/skyhong2002/localplaud> (MIT). Work is on branch `feat/core-pipeline` (PR #6, CI green, 90 tests).
- **Production is LIVE on SkyLabMac** (M4 Mac mini): launchd service `com.localplaud.agent` runs `localplaud run`; reverse-proxied by the existing Caddy at **https://plaud.observe.tw** (basic_auth). Local ASR = mlx-whisper (Metal); LLM/embeddings = ollama.
- **Real account verified**: the official Open API provider is live in production (OAuth auto-refresh verified). Notably it returns the account's **full history (~750 recordings)** — the old api-apse1 web listing only showed the most recent ~200 — so the backlog sync is correspondingly bigger. Cloud transcripts/summaries are being mirrored (`prefer_cloud_artifacts = true`), skipping local re-transcription where Plaud already did the work.
- Dev env on SkyLabMac: `~/Projects/localplaud` (venv, ffmpeg static, config.toml, `.env`). Claude Code CLI installed (`~/.local/bin/claude`).

## TODO — prioritized

### ✅ DONE (2026-07-10) — P0: official Open API is now the default provider
`plaud.provider = "official"` (default): OAuth via the official Plaud CLI
(`localplaud auth login` wraps it; tokens in `~/.plaud/tokens.json`,
auto-refresh implemented in `plaud/oauth.py`, verified live — both tokens
rotate, 24h expiry). `PlaudOfficialClient` (`plaud/official.py`) mirrors
Plaud's own transcripts (with speaker names) + summaries from
`/open/third-party/files/{id}`; with `pipeline.prefer_cloud_artifacts = true`
the pipeline reuses them and skips local re-transcription. api-apse1 is now
optional enrichment (`plaud.apse1_enrichment`, needs a pasted session) for
`version`/`file_md5`/`edit_time`/`is_trash`. Full API notes: `docs/plaud-api.md`.
Largely closes issues #8 and #9.

### P1 — Ongoing sync robustness
- ~~api-apse1 refresh-flow stopgap (`pld_ut` cookie)~~ superseded by the
  official provider. Remaining nice-to-have: a native PKCE flow inside
  localplaud (drop the Node.js dependency for `auth login`).

### P1 — Deploy the other two machines
- **CCLabPC** (nvplaud.observe.tw, NVIDIA/CUDA): docker `gpu` profile or native; needs user in `docker` group. DNS already points here.
- **Oracle** (plaud.skyhong.tw, aarch64 CPU): `cpu` slim image (already builds/runs there) + Caddy vhost; cloud ASR.
- Pattern to reuse: append a `<domain> { basic_auth … ; reverse_proxy 127.0.0.1:8080 }` block to that host's Caddyfile (SkyLabMac already done this way).

### P2 — Product polish (from earlier review)
- UI: Mind Map tab, folders/tags in the sidebar, export PDF/SRT (currently .md only), SPA-style pane swapping.
- Cloud ASR providers (Deepgram/OpenAI/AssemblyAI) real-key verification; pyannote diarization with a HF token.
- Programmatic login (issue #8) — solved by P0's OAuth.

### Housekeeping
- Merge PR #6 to `main` (blocked: agent can't self-merge).
- Optional: root LaunchDaemon so production starts on boot without login (needs sudo).

## Ops quick-reference (SkyLabMac)
- Update prod: `git -C ~/Projects/localplaud pull && launchctl kickstart -k gui/$(id -u)/com.localplaud.agent`
- Logs: `~/Projects/localplaud/data/service.{out,err}.log`
- Service: `launchctl list | grep localplaud`; plist at `~/Library/LaunchAgents/com.localplaud.agent.plist`
- Caddy vhost: block for `plaud.observe.tw` in `/usr/local/etc/caddy/Caddyfile` (basic_auth user `sky`); reload `caddy reload --config /usr/local/etc/caddy/Caddyfile`
- Session/creds: `~/Projects/localplaud/.env` (git-ignored)
