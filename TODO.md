# localplaud — status & TODO

Working notes for continuing development (synced across machines via git).
No secrets here — those live in `.env` / the Caddyfile, never committed.

## Status snapshot (2026-07-10)

- Full app built & published: <https://github.com/skyhong2002/localplaud> (MIT). Work is on branch `feat/core-pipeline` (PR #6, CI green, 90 tests).
- **Production is LIVE on SkyLabMac** (M4 Mac mini): launchd service `com.localplaud.agent` runs `localplaud run`; reverse-proxied by the existing Caddy at **https://plaud.observe.tw** (basic_auth). Local ASR = mlx-whisper (Metal); LLM/embeddings = ollama.
- **Real account verified**: auth + `/file/temp-url` → signed-S3 MP3 download works; ~200 recordings syncing/transcribing in the background.
- Dev env on SkyLabMac: `~/Projects/localplaud` (venv, ffmpeg static, config.toml, `.env`). Claude Code CLI installed (`~/.local/bin/claude`).

## TODO — prioritized

### P0 — Switch to Plaud's official MCP / Open API (biggest win)
Replaces the reverse-engineered `api-apse1` client for the core loop, with sanctioned OAuth that **auto-refreshes** (no more 14h session re-paste, no WAF 403).
- Official MCP: remote `https://mcp.plaud.ai/mcp`, local `npx -y @plaud-ai/mcp@latest`. Docs: <https://docs.plaud.ai/plaud-mcp-cli/mcp>, blog: <https://www.plaud.ai/blogs/news/introducing-plaud-mcp-and-cli>.
- Underlying REST: `GET platform.plaud.ai/developer/api/open/third-party/files/?page=&page_size=` and `.../files/{id}`. OAuth token endpoints under `platform.plaud.ai/developer/api/oauth/third-party/*`; tokens cached in `~/.plaud/tokens.json`.
- Tools/capabilities (read-only): `list_files`, `get_file` (returns a **24h presigned audio URL** + transcript `source_list` + Markdown `note_list`), `get_transcript`, `get_note`, `get_current_user`.
- **Plan**: add a `plaud-official` client provider (OAuth Open API) behind the same interface as `PlaudClient`; make it the default. Keep the api-apse1 client as optional enrichment for change-detection fields the Open API lacks (`version` / `file_md5` / `edit_time` / `is_trash`, tags/scene). Reuse `get_transcript`/`get_note` to mirror Plaud's own transcript+notes (largely closes issue #9) and skip local re-transcription when desired.
- Full analysis: `scratchpad/plaud-recon/MCP.md`.

### P1 — Ongoing sync robustness
- Until the MCP switch lands: the pasted access token expires ~14h. The `pld_ut` cookie is a **refresh token** (`auth_method: token_refresh`) → a refresh flow against api-apse1 is feasible as a stopgap. (Superseded once P0 lands.)

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
