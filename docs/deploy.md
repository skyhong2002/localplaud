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
   - Your Plaud session (`LOCALPLAUD_PLAUD__API_BASE`, `LOCALPLAUD_PLAUD__COOKIE`,
     and `LOCALPLAUD_PLAUD__EXTRA_HEADERS` if needed) — see
     [plaud-api.md](plaud-api.md).
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
  # [diarize] provider = "pyannote" (plus HF token/model acceptance)
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
and keep the diarization profile enabled.

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

The UI has no auth of its own beyond an optional shared token. If it's reachable
from anything but localhost:

- Set `LOCALPLAUD_API__AUTH_TOKEN` (required on every request), **and/or**
- Add `basic_auth` to the `Caddyfile` in front of `reverse_proxy`.

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
