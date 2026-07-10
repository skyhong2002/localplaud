# Execution profiles and remote workers

localplaud imports the existing TOML/environment provider choices into a durable
`system-default` execution profile on `localplaud init`. It also creates Apple,
NVIDIA, CPU, OpenAI Cloud, deliberately text-only OpenAI-compatible, and Remote GPU
starting profiles, but never selects them automatically. The compatible and remote
starters use loopback placeholder endpoints that must be edited and health-checked;
their model catalogs declare only the capabilities they actually route. Check
`GET /api/hardware` and run provider health/speech benchmarks before selecting one.
The system boundary permits these explicitly selected starters, but its active stage
routes still exactly mirror the existing config; adding a starter never selects it
or creates an implicit external fallback. Apple/NVIDIA/CPU starters tighten their
own policy to `no_egress`.

Resolution order is deterministic:

```text
system default -> folder/AutoFlow rule -> template default -> recording override
```

All four layers are persisted and exercised by the pipeline. A recording stores its
folder/rule context, a note template may name a default profile, and the recording
may have its own override. AutoFlow remains a P2 producer of rule context rather than
a second provider system. Every executed attempt stores the complete resolved
snapshot. Profile edits do not rewrite `stage_attempts` or artifact provenance;
profile changes become active only when a real new attempt begins, so reused
artifacts are never relabelled.

Profiles can declare `no_egress`, `allowed_providers`, `cost_ceiling`,
`timeout_ceiling_seconds`, a bounded `retry` policy, `quality_floor`, explicit
fallback routes, and stage options. Resolution applies the same capability, quality,
timeout, cost, and egress checks to every fallback. Connection metadata is
materialized before acceptance, so naming an external connection cannot weaken a
no-egress profile. Secrets are `env:` references (management responses mask them),
never values stored in a profile.

## Headless APIs

- `GET/POST /api/provider-connections`
- `GET/POST /api/provider-models`
- `GET /api/capabilities`
- `POST /api/provider-connections/{id}/health`
- `GET/POST/PUT /api/execution-profiles`
- `GET /api/execution-profiles/resolve?stage=...&file_id=...`
- `PUT /api/files/{id}/execution-profile`
- `GET /api/files/{id}/stage-runs`
- `GET /api/hardware`

OpenAI-compatible audio, text/chat, and embedding capabilities must be registered
separately. Registering one shape never makes the other shapes routable. Custom
base URL, headers, timeout, input limit, model, and an `env:` secret reference can
be selected per route. Inline secrets are rejected.

`codex-local` is an experimental external-egress text capability for a trusted
single-user installation. It is disabled by default and invokes only `codex exec`
with stdin, an ephemeral session, an empty temporary workspace, ignored user/rule
configuration, and a read-only sandbox. It neither reads nor copies Codex auth
tokens and is not an OpenAI-compatible endpoint.

## Remote worker protocol v1

`localplaud.providers.remote_worker_server` supplies a reference authenticated
FastAPI worker and SQLite/WAL job store. The protocol provides:

- capability/model handshake;
- HMAC-authenticated submission/status/cancellation;
- durable idempotency and conflict detection across restarts;
- expiring runner claims with heartbeats, crash recovery, and stale-owner rejection;
- monotonic progress and structured retryable errors that can be safely requeued;
- immutable completed artifacts with SHA-256 manifests; and
- recursive rejection of Plaud credential fields.

For remote ASR, alignment, and diarization, the main service issues a short-lived,
file-scoped signed audio URL. Text notes, mind maps, Ask, correction, and embeddings
use checksum-verified JSON jobs without audio input. The reference runner includes
durable generic text, correction, embedding, and ASR handler builders; deployments
may register benchmarked alignment/diarization handlers under the same contracts.
The worker receives that URL, checksum, model, language option, and job identifiers;
it never receives Plaud OAuth credentials or the main Web API token. The returned
inline transcript is accepted only when its canonical JSON checksum matches.

Production deployment must route `/api/worker-input/` through the reverse proxy
without interactive basic authentication while retaining its signature validation.
Use a distinct worker HMAC token and input-signing secret, and configure an explicit
worker fetch-host allowlist. Validate the worker with
a self-owned NVIDIA host and a separately isolated rentable GPU before marking those
acceptance rows complete.
