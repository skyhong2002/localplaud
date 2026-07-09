# ADR 0004: Deployment — one repo, compose profiles `mac` / `gpu` / `cpu`

Status: Accepted

## Context

The same codebase must run on (at least) three very different hosts:

1. **Mac mini (Apple Silicon).** Docker on macOS runs in a Linux VM, so
   containers cannot use Metal — GPU-accelerated ASR cannot live inside
   Docker on a Mac.
2. **Linux box with an NVIDIA GPU.** CUDA works in containers via the
   NVIDIA container toolkit.
3. **Weak CPU-only Linux box** (small VPS). Local Whisper at useful
   model sizes is impractically slow.

Each host also wants HTTPS on its own domain without hand-managed certs.

## Decision

One repository, one `docker-compose.yml`, three **compose profiles**:

- **`mac`**: core services (poller, worker, API/UI, DB) in Docker, but ASR
  runs *outside* the container — either on the host (whisper.cpp /
  mlx-whisper reached over localhost) or via a cloud provider — because
  Metal cannot be passed into Docker. The ASR registry (ADR 0003) makes
  this a config choice, not a code path.
- **`gpu`**: CUDA-enabled worker image; requires the NVIDIA container
  toolkit and reserves the GPU for faster-whisper.
- **`cpu`**: no local ASR at all; `asr.provider` is a cloud provider
  always-on, so a weak box only does I/O, orchestration, and storage.

Every profile fronts the UI with **Caddy** as the reverse proxy —
automatic HTTPS via Let's Encrypt, one domain per host (e.g.
`plaud.example.tw`), config is a few lines of Caddyfile.

## Consequences

- `docker compose --profile mac up` (etc.) is the whole deployment story;
  profile choice maps 1:1 to hardware reality instead of pretending one
  image fits all.
- The `mac` profile splits ASR from the containers, so the host needs a
  small extra setup step (install whisper.cpp or set a cloud key); this
  is unavoidable physics, documented rather than hidden.
- Caddy adds one lightweight container but removes all certificate
  toil; per-host `public_url` config keeps links correct behind the proxy.
- CI can build the plain and CUDA images from the same Dockerfile
  (multi-stage / build args), keeping drift between profiles low.
