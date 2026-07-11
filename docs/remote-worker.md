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
  "base_url": "https://nvplaud.observe.tw/",
  "token_env": "LOCALPLAUD_WORKER_TOKEN",
  "timeout": 120,
  "job_timeout": 3600
}
```

Do not put the token itself in the connection, model, profile, job, or repository.

## Execution and policy

Profiles select a connection whose `execution_target` is `remote_worker`. The
resolver rejects that selection under a local-only/no-egress policy. Supported
remote stages are transcription, diarization, notes, mind maps, and embeddings.
The controller sends only audio or the canonical transcript required by that stage,
polls with bounded exponential backoff, and reuses the idempotency key on reconnect.

The protocol and same-process integration are covered by automated tests. NVIDIA
and rentable-GPU deployment acceptance remain separate hardware validation tasks;
do not claim GPU acceleration until those hosts complete the benchmark matrix.
