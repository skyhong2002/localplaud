# Remote worker protocol v1

`localplaud-worker` lets a controller send individual processing stages to a
self-owned or explicitly selected GPU host. It does not send Plaud OAuth state,
application Settings, or provider credentials.

## Contract

The authenticated API is mounted at `/api/worker/v1`:

- `GET /capabilities` — protocol/version handshake and stage/model catalog.
- `POST /jobs` — idempotent submission with a caller-generated key.
- `GET /jobs/{id}` — durable status, progress, artifacts, or structured error.
- `POST /jobs/{id}/cancel` — durable cancellation intent.
- `GET /jobs/{id}/artifacts/{name}` — checksummed result download.

Jobs and artifact metadata live in SQLite, so queued/running work is recovered after
an API restart. Artifacts carry SHA-256 digests and the controller verifies every
download before accepting it. Errors include a stable code and `retryable` flag.

Inputs are stage-specific `inline_json`, `inline_base64`, or short-lived `url`
references. Credential-shaped fields such as OAuth/access/refresh tokens, cookies,
authorization, API keys, and Plaud credentials are rejected recursively. URL fetches
reuse localplaud's SSRF validation and do not follow redirects.

## Authentication

Set the same high-entropy value on the worker and controller:

```bash
LOCALPLAUD_WORKER_TOKEN='generate-a-long-random-value'
```

The worker reads this only from the environment. A remote-worker provider connection
uses configuration like:

```json
{
  "base_url": "http://<worker-tailnet-address>:8081",
  "token_env": "LOCALPLAUD_WORKER_TOKEN",
  "timeout": 120,
  "job_timeout": 3600
}
```

A private overlay address (Tailscale, WireGuard, LAN) is fine for a plain-HTTP
`base_url`; anything reachable from the public internet must sit behind HTTPS.

Do not put the token itself in the connection, model, profile, job, or repository.

## Execution and policy

Profiles select a connection whose `execution_target` is `remote_worker`. The
resolver rejects that selection under a local-only/no-egress policy. Supported
remote stages are transcription, diarization, notes, mind maps, and embeddings.
The controller sends only audio or the canonical transcript required by that stage,
polls with bounded exponential backoff, and reuses the idempotency key on reconnect.

The protocol and same-process integration are covered by automated tests.

## The production topology (mac-wsl-hybrid)

The production deployment is one controller plus one worker. Since 2026-08-19,
the system default sends ASR to the WSL CUDA worker after a clean
`large-v3-turbo` validation run:

- **Controller**: the M4 Mac mini runs the Web App, polling, durable scheduler,
  WhisperX word alignment, transcript correction, and library Ask. Mac MLX ASR
  remains an explicit alternate profile, not the production default.
- **Worker**: a WSL2 host with an RTX 5060, running the pinned CUDA image
  (PyTorch 2.8 / CUDA 12.8 / TorchCodec 0.7 / pyannote 4), reached over
  Tailscale at its tailnet address on port 8081. The execution profile
  (`mac-wsl-hybrid`, the system default) dispatches **transcribe, diarize,
  summarize, mind-map, and embed** to it. Transcription uses faster-whisper
  `large-v3-turbo` with `device=cuda`.
- **GPU serialization**: the controller holds a process-wide GPU lock so only
  one GPU-bound remote stage runs at a time — concurrent pyannote jobs
  deadlocked the single card. The worker is the throughput bottleneck by design.

The worker container must run `localplaud serve`, not `localplaud run`. The
controller owns Plaud polling and backlog scheduling; `serve` exposes the
authenticated worker API without starting a second autonomous poller on WSL.
The production-only Compose override pins that command and is intentionally
kept beside the worker's local secrets rather than committed.

Operational caveats learned the hard way:

- Keep controller and worker on the **same code/protocol revision**. The worker
  bind-mounts `src/` over the image, so a `git pull` on the worker host changes
  behavior without an image rebuild — and forgetting to pull leaves the two
  sides skewed.
- After fixing worker-side code, **clear the affected durable `remote_jobs`
  rows** on the worker: completed-with-bad-output jobs are otherwise replayed
  from cache thanks to idempotency keys.
- Remote embedding requires the worker to attest the exact embedding model;
  a model mismatch is a hard, non-retryable error rather than silently mixing
  vector spaces.
- **Ollama can silently fall back to CPU and stay there.** Its scheduler logs a
  healthy GPU (`library=CUDA`, `model fits`, a plausible VRAM prediction) and
  only the llama-server child reports the real failure —
  `ggml_cuda_init: failed to initialize CUDA: CUDA driver version is
  insufficient for CUDA runtime version`. Inference then runs at CPU speed
  (~3-5 tok/s instead of ~75) and the only cheap symptom is `ollama ps` showing
  `100% CPU` and an inflated model SIZE, because a CPU-resident KV cache is
  counted differently. On 2026-08-01 this made every worker summarize/mind-map
  run 40-230 minutes and produced a run of "timed out" mind-map failures.
  Diagnose with `docker exec <ollama> ollama ps` (PROCESSOR column) and
  `docker exec <ollama> nvidia-smi`; "GPU access blocked by the operating
  system" from a container whose sibling CUDA container still works means that
  container's GPU mounts went stale — recreate it with the recipe below (models
  live in the named volume and survive). Verify the fix by the PROCESSOR column
  reading `100% GPU`, not by the scheduler log lines.

The worker's Ollama is started outside Compose, so its configuration only
exists in the running container. The exact recreate is:

```sh
docker rm -f localplaud-ollama
docker run -d --name localplaud-ollama \
  --restart unless-stopped --gpus all \
  --network localplaud_default \
  -v localplaud_ollama:/root/.ollama \
  -e OLLAMA_CONTEXT_LENGTH=8192 -e OLLAMA_KEEP_ALIVE=2m \
  -e OLLAMA_HOST=0.0.0.0:11434 \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,utility -e NVIDIA_VISIBLE_DEVICES=all \
  ollama/ollama:0.31.2
```

Port 11434 is deliberately unpublished: only sibling containers on
`localplaud_default` reach it, as `http://localplaud-ollama:11434`. Recreating
it fails any in-flight remote job with a `500`; those retry on the normal
stage-retry path.

Multi-host *web* deployments and rentable-GPU validation were explicitly
dropped on 2026-07-31; this single controller + single worker pair is the
supported production topology. Cross-host artifact-parity validation for this
pair (product-workflow acceptance scenarios 8 and 12) is still open — see
`TODO.md`.
