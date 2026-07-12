# Deploying localplaud

localplaud ships one repo that runs across very different hardware via Docker
Compose **profiles**. A bundled Caddy reverse proxy terminates HTTPS for your
domain automatically. See [ADR 0004](adr/0004-deployment-profiles.md) for the
rationale.

| Profile | Hardware                     | ASR                                   |
| ------- | ---------------------------- | ------------------------------------- |
| `cpu`   | CPU-only machine             | local turbo where practical; explicit cloud opt-in |
| `gpu`   | NVIDIA + CUDA                | local Whisper large-v3-turbo           |
| `mac`   | Apple Silicon                | local MLX large-v3-turbo on the host   |

The subscription-independent reference profile uses local Whisper large-v3-turbo plus
alignment and diarization. A cloud ASR remains possible, but is an explicit cost and
privacy choice rather than an automatic fallback.

## Before you start (per host)

1. **DNS**: point your domain's A/AAAA record at the machine's public IP.
   Caddy needs this to obtain a certificate.
2. **Ports**: open 80 and 443 to the internet (Caddy). No other port needs to
   be public — the app listens on 8080 behind Caddy.
3. **Secrets**: copy `.env.example` → `.env` and fill in:
   - `DOMAIN` and `ACME_EMAIL`
   - Authorize either the official Open API with `localplaud auth login` or the
     official MCP with `npx -y @plaud-ai/mcp@latest install`.
   - ASR keys for whichever provider you use.
4. **Bootstrap** installs Docker (+ optionally the NVIDIA toolkit):
   ```bash
   ./scripts/deploy/bootstrap.sh          # Docker + compose
   ./scripts/deploy/bootstrap.sh --gpu    # + nvidia-container-toolkit (Linux)
   ```

## Bring it up

```bash
git clone https://github.com/skyhong2002/localplaud && cd localplaud
cp .env.example .env && $EDITOR .env
cp config.example.toml config.toml && $EDITOR config.toml

docker compose --profile <cpu|gpu|mac> up -d --build
./scripts/deploy/smoke-test.sh http://localhost:8080
```

Then browse to `https://$DOMAIN`.

The Web App ships its browser runtime inside the Python package. Normal operation
does not require access to a JavaScript CDN, which is suitable for private or
offline deployments.

---

## The reference three-machine setup

### 1. Mac mini (Apple Silicon) → `plaud.observe.tw`

Docker on macOS **cannot** pass the Metal GPU into a container, so on-device
Whisper must run on the host. Two options:

- **Cloud ASR in Docker** (simplest): use the `mac` profile with
  `LOCALPLAUD_ASR__PROVIDER=deepgram` (or openai/assemblyai).
- **On-device Metal ASR** (fastest, private): run the app natively and let
  Caddy (in Docker) proxy to it:
  ```bash
  uv sync --extra mlx --extra diarize --extra local-llm
  # config.toml: provider = "mlx-whisper"
  # model = "mlx-community/whisper-large-v3-turbo"
  # [diarize] provider = "pyannote", device = "cpu" (plus HF token/model acceptance)
  uv run localplaud run                        # host app on :8080
  docker compose --profile mac up -d caddy     # HTTPS only; Caddyfile → host.docker.internal:8080
  ```
  (Point the Caddyfile's `reverse_proxy` at `host.docker.internal:8080` for
  this variant.)

### 2. NVIDIA Ubuntu → `nvplaud.observe.tw`

```bash
./scripts/deploy/bootstrap.sh --gpu
docker compose --profile gpu up -d --build
```

Uses `Dockerfile.cuda` (CUDA 12.8 + PyTorch/torchaudio 2.8 + faster-whisper on GPU + pyannote for
diarization). Verify the GPU is visible: `docker compose exec localplaud-gpu nvidia-smi`.
Set `[asr] provider = "faster-whisper"`, model `large-v3-turbo`, device `cuda`,
and keep the diarization profile enabled with `[diarize] device = "cuda"`.

True word-level forced alignment is available through the separately selectable
`align:whisperx` / `wav2vec2-auto` catalog entry. Native installs require the
optional runtime:

```bash
uv sync --extra faster-whisper --extra forced-align --extra diarize
```

The bundled CUDA image does not yet install the `forced-align` extra. Selecting the
WhisperX profile in that image therefore reports an actionable degraded align stage;
build a derived image with the extra before enabling it. Keep provider timestamps as
the production selection until the language-specific model has been benchmarked on
owned Taiwan Mandarin and Mandarin/English recordings.

For a fully local text path on the same NVIDIA host, enable the separately named
Ollama profile and install an explicit model. Ollama is reachable only on the
private Compose network; port 11434 is not published on the host:

```bash
docker compose --profile gpu --profile ollama up -d --build
docker compose exec ollama-gpu ollama pull qwen3:4b-instruct-2507-q4_K_M
```

Point the stage-scoped LLM selections at the bundled service:

```toml
[llm]
provider = "ollama"

  [llm.ollama]
  host = "http://ollama-gpu:11434"
  model = "qwen3:4b-instruct-2507-q4_K_M"
```

The `ollama` profile is opt-in because provider choice and data-egress policy are
explicit localplaud settings. Its model cache is durable in the `ollama_data`
volume. On smaller GPUs, keep ASR, diarization, and text generation sequential and
select a model that leaves enough memory for the configured context window.
Use an instruction model for correction and structured notes: the floating
`qwen3:4b` tag may resolve to a thinking variant whose visible reasoning is not
valid structured output even when thinking is disabled by the client.

> **Running CUDA natively (no Docker)**: the NVIDIA driver alone isn't enough —
> CTranslate2 needs cuBLAS and cuDNN 9. Install the `cuda` extra
> (`uv sync --extra cuda`), which pulls `nvidia-cublas-cu12` + `nvidia-cudnn-cu12`,
> and ensure they're on `LD_LIBRARY_PATH`. The Docker image bundles them already.

### 3. Oracle Cloud (aarch64, 2 vCPU) → `plaud.skyhong.tw`

The always-on poller/downloader. It is too weak to be the preferred local turbo
inference worker. For subscription independence, let it ingest/store while a Mac or
CUDA worker processes the shared queue. A cloud ASR API is an explicit alternative:

```bash
./scripts/deploy/bootstrap.sh
# Explicit cloud choice only:
# LOCALPLAUD_ASR__PROVIDER=deepgram + LOCALPLAUD_ASR__DEEPGRAM__API_KEY=...
docker compose --profile cpu up -d --build
```

## Securing an exposed instance

If the UI is reachable from anything but localhost:

- Serve it through HTTPS and set both `LOCALPLAUD_API__LOGIN_PASSWORD` and a long,
  random `LOCALPLAUD_API__SESSION_SECRET` to enable the built-in `/login` page.
- Set `LOCALPLAUD_API__AUTH_TOKEN` separately when non-browser API clients need
  Bearer or `X-Auth-Token` access.
- The reverse proxy normally only terminates HTTPS; upstream authentication can
  still be used when a deployment deliberately delegates identity to it.

See [ADR 0006](adr/0006-security-posture.md).

## Operating it

- Logs: `docker compose --profile <p> logs -f`
- One-off sync: `docker compose exec <service> localplaud poll --once`
- The `run` command already polls on a schedule, processes the backlog, and
  serves the UI; nothing else to cron.
- To attach a separate GPU worker, configure its dedicated
  `LOCALPLAUD_WORKER_TOKEN`, expose the authenticated worker API through HTTPS,
  then register it in Settings. See [remote-worker.md](remote-worker.md).

## Updating

```bash
git pull
docker compose --profile <p> up -d --build
```

## Running natively (no Docker)

Everything works without Docker too:

```bash
uv sync --extra faster-whisper --extra local-llm
uv run localplaud run
```

Put it behind your own reverse proxy, or run a standalone Caddy/nginx pointing
at `:8080`.
