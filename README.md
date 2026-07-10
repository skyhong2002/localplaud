<p align="center">
  <img src="src/localplaud/api/static/wordmark.svg" alt="localplaud" height="52">
</p>

<h1 align="center"></h1>

<p align="center">
  <b>A self-hosted Plaud clone.</b> Keep recording with your physical Plaud device,
  but mirror everything to a machine you own and run your own transcription,
  speaker diarization, summaries, and Q&amp;A — your audio never leaves home.
</p>

<p align="center">
  <a href="#how-it-works">How it works</a> ·
  <a href="#quickstart">Quickstart</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#asr-providers">ASR providers</a> ·
  <a href="#deploying-to-your-own-machines">Deploy</a>
</p>

---

## Why

Plaud's hardware is great; its cloud is a black box. **localplaud** treats the
official Plaud cloud as the source of truth for your *raw audio* and rebuilds
everything the cloud does — download, transcode, transcribe, diarize,
summarize, template notes, and ask-your-recordings search — on hardware you
control. It is a **mirror + local processing layer**, not a replacement for the
Plaud device or its app.

> You keep using the Plaud device and its app exactly as before. localplaud
> polls the Plaud cloud on a schedule, pulls down new/changed recordings, and
> processes them locally.

## Status

Working end to end: cloud polling over **Plaud's official Open API** (OAuth,
auto-refreshing — sign in once with `localplaud auth login`), audio download,
local transcription (6 ASR backends), diarization, LLM summaries, embeddings +
Q&A, an in-page audio player, and mirroring of Plaud's own transcripts (with
speaker names) and summaries so you can skip local re-transcription entirely.
Runs natively and in Docker (mac/gpu/cpu profiles), verified on three
architectures. The reverse-engineered web API remains available as an optional
enrichment source / fallback provider — see the
[open issues](https://github.com/skyhong2002/localplaud/issues).

## How it works

```
  Plaud device ──sync──► Plaud app ──upload──► Plaud cloud
                                                   │
                                    poll + download │  (localplaud, read-only)
                                                   ▼
   ┌─────────────────────────── localplaud ───────────────────────────┐
   │  poller ─► store (audio on disk + SQLite) ─► worker pipeline:     │
   │             convert ─► transcribe ─► diarize ─► summarize ─► index │
   │                                          │                        │
   │                              api / web UI (search + Q&A)          │
   └───────────────────────────────────────────────────────────────────┘
```

- **poller** — polls the Plaud cloud API, detects new or updated files (via
  `version`/`version_ms`), downloads the `.opus` audio.
- **store** — audio bytes on the filesystem; metadata, transcripts, summaries
  and embeddings in SQLite.
- **worker** — the local pipeline: `opus → wav` (ffmpeg) → ASR → diarization →
  LLM summary/notes → embeddings for Q&A.
- **api / ui** — a small FastAPI + HTMX app to browse, search, and ask
  questions across your recordings.

## Quickstart

Requirements: **Python 3.11+**, **ffmpeg**, and (for local ASR) a Whisper
backend. [uv](https://github.com/astral-sh/uv) is recommended.

```bash
git clone https://github.com/skyhong2002/localplaud
cd localplaud

# install (choose extras for the ASR you want — see below)
uv sync --extra faster-whisper          # local ASR, CPU/CUDA
#   or: pip install -e ".[faster-whisper]"

cp config.example.toml config.toml      # edit to taste
cp .env.example .env                    # put secrets here (git-ignored)

localplaud init                         # create the database
localplaud auth login                   # one-time browser OAuth (official API)
localplaud auth check                   # verify your Plaud session works
localplaud poll --once                  # pull the file list + download audio
localplaud work --once                  # run the pipeline on downloaded files
localplaud serve                        # web UI at http://localhost:8080
```

Or run everything as a daemon (poll on a schedule + process continuously):

```bash
localplaud run
```

### Commands

| Command | What it does |
| --- | --- |
| `localplaud init` | Create the database + data dirs |
| `localplaud auth login` / `auth check` | One-time OAuth sign-in / verify the session |
| `localplaud doctor` | Check ffmpeg + your ASR/LLM/embedding providers + auth |
| `localplaud poll [--once]` | Sync the cloud listing + download audio |
| `localplaud work [--once] [--force]` | Run the pipeline on downloaded recordings |
| `localplaud run` | Poll + process + serve, all together |
| `localplaud ls` / `status` | List recordings / counts by stage |
| `localplaud ask "…"` | Q&A across all transcripts |
| `localplaud reprocess <id>` | Re-run the pipeline on one recording |
| `localplaud export <id> [-o …]` | Export a recording to Markdown |
| `localplaud serve` | Web UI only |

### Your Plaud session

localplaud never sees your Plaud password. The default provider is **Plaud's
official Open API**: run `localplaud auth login` once — it opens your browser
for OAuth (via the official Plaud CLI, Node.js ≥ 20) and caches an
auto-refreshing token set in `~/.plaud/tokens.json`. That's it; the session
keeps itself alive.

Optionally, you can *also* paste a legacy web session (`api-*.plaud.ai`).
localplaud then enriches its sync metadata with fields the Open API doesn't
expose (`version`, `file_md5`, `edit_time`, `is_trash`) — useful for detecting
edits/deletions faster, but not required:

1. Log in to <https://web.plaud.ai>, open DevTools → Network, and find an
   authenticated request to `api-*.plaud.ai` (e.g. `GET /user/me`).
2. Copy it as cURL and run `localplaud auth import` to turn it into `.env`
   lines (this session expires after ~14 h).
3. Setting `plaud.provider = "apse1"` makes this the primary provider instead.

`localplaud auth check` confirms whichever provider is configured. See
[`docs/plaud-api.md`](docs/plaud-api.md) for API details.

## Configuration

All configuration lives in `config.toml` (copy from
[`config.example.toml`](config.example.toml)). Every value can be overridden by
an environment variable prefixed `LOCALPLAUD_` with `__` between levels, so
**secrets stay out of the file** and in `.env`:

```bash
LOCALPLAUD_PLAUD__COOKIE="Authorization: Bearer ..."
LOCALPLAUD_ASR__OPENAI__API_KEY="sk-..."
LOCALPLAUD_DIARIZE__HF_TOKEN="hf_..."
```

## ASR providers

ASR is fully pluggable, and **local and cloud engines are equal first-class
choices** — pick whichever gives you the best accuracy / speaker separation,
not just as a weak-machine fallback. Set `asr.provider`, list `asr.fallback`
providers to try if the primary can't run, and drop in API keys for the cloud
options.

| Provider          | Type   | Runs on                    | Diarization |
| ----------------- | ------ | -------------------------- | ----------- |
| `faster-whisper`  | local  | CPU / NVIDIA CUDA          | via pyannote |
| `whispercpp`      | local  | Apple Silicon (Metal) / CPU | via pyannote |
| `mlx-whisper`     | local  | Apple Silicon (MLX)        | via pyannote |
| `openai`          | cloud  | any                        | via pyannote |
| `deepgram`        | cloud  | any                        | built-in    |
| `assemblyai`      | cloud  | any                        | built-in    |

## Deploying to your own machines

localplaud ships a single Docker Compose file with **profiles** so one repo runs
on very different hardware:

| Profile | Target machine        | ASR                                   |
| ------- | --------------------- | ------------------------------------- |
| `mac`   | Apple Silicon Mac     | local Whisper (Metal, outside Docker) |
| `gpu`   | NVIDIA (CUDA)         | local Whisper in-container (CUDA)      |
| `cpu`   | small/cloud CPU boxes | cloud ASR API                         |

A bundled Caddy reverse proxy terminates HTTPS for your domain automatically.
See [Deployment](#deploying-to-your-own-machines) below and `docs/deploy.md`.

## Development

```bash
uv sync --extra dev
ruff check . && pytest
```

## Security

- Secrets (Plaud session, API keys, HF token) go in `.env` or environment
  variables — **never** in a committed file. `config.toml`, `.env`, `*.cookie`
  and `*.token` are git-ignored.
- localplaud only ever issues **read-only** requests against the Plaud cloud,
  and refuses to fetch non-`https` or private-IP URLs (SSRF-guarded), with
  bounded downloads.
- The web UI binds to `127.0.0.1` by default. **Before exposing it**, set
  `api.auth_token` (or `LOCALPLAUD_API__AUTH_TOKEN`) and/or put auth in front
  (Caddy `basic_auth`). See [ADR 0006](docs/adr/0006-security-posture.md).

## License

[MIT](LICENSE) © 2026 Sky Hong

> localplaud is an independent, unofficial project and is not affiliated with,
> endorsed by, or connected to Plaud. It only accesses your own account's data.
