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

Queued and running jobs retain their input payloads for restart recovery. Once a
job succeeds, fails, or is cancelled, the worker releases input values while
retaining their names, types, checksums, job provenance, and result artifacts.
Terminal manifests carry `input_payloads_released: true` and are audit records,
not executable requests. Resubmitting a failed job restores the complete request
before retrying; successful idempotent requests continue to return cached results.
Released SQLite pages can be reused by later jobs; the database file does not
automatically shrink on disk.

## Offline storage reclamation

Status polling and artifact downloads do not load the uploaded input manifest.
For a legacy worker DB containing retained audio, stop dispatch and wait for all
queued/running work to finish, then stop the worker before maintenance. Keep the
original recording files and all user edits.

Create a separate verified candidate rather than running a full in-place VACUUM
when host space cannot accommodate another copy of the original database:

```bash
python3 scripts/maintenance/compact_worker_db.py \
  --source data/localplaud.db \
  --output data/localplaud.compact.db \
  --acknowledge-offline \
  --space-guard-path /mnt/c
```

On WSL, `/mnt/c` checks the physical Windows volume; elsewhere select the actual
backing filesystem. The default free-space floor is 10 GiB. The tool refuses
active jobs, output collisions and unsupported schemas, preserves all tables and
result artifacts, and removes only terminal job input values. It checks schema,
row counts, streaming hashes, foreign keys and SQLite integrity before reporting
success. Source rows remain unchanged, although SQLite may checkpoint WAL
sidecars on close. The tool never installs the candidate. Individual SQLite
values above 512 MiB are rejected to bound memory use; unusually large legacy
records require a separate maintenance plan.

Checkpoint and close every connection before an offline swap. Retain the old
file until the replacement passes worker health, cached-artifact download and a
new job check. Do not copy or swap a live SQLite main file without its committed
WAL. Long-running read transactions can prevent WAL truncation. Incremental
vacuum is enabled on newly compacted candidates, but releasing SQLite pages
alone does not return space from a WSL virtual disk to Windows: that also requires
filesystem trim and offline VHD compaction. Never unregister WSL or delete its VHD.

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

Speech detection must also be enabled in the worker's own `[asr.vad]` settings.
The CUDA image includes shared Silero VAD; verify the installed dependency and
effective worker configuration after deployment. Controller-only configuration
does not activate remote VAD. See [speech-quality.md](speech-quality.md).

Operational caveats learned the hard way:

- A running WSL container is not proof that CUDA works. If host
  `/usr/lib/wsl/lib/nvidia-smi` succeeds but the worker container reports
  `GPU access blocked by the operating system`, first check that no jobs are
  running, then restart the worker and Ollama containers. Verify CUDA from
  inside both containers and complete a real transcription through the
  controller before requeueing exhausted work. Keep the existing model/data
  volumes and take a consistent controller database backup before bulk recovery.
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
