# 6. Security posture

Status: Accepted

## Context

localplaud handles a user's private recordings, authenticates to the Plaud
cloud with a pasted token, downloads bytes from URLs found in API responses,
and serves a web UI. An independent review surfaced concrete risks worth
addressing before wider use.

## Decision

- **Web UI is loopback-by-default with an optional token gate.** `api.host`
  defaults to `127.0.0.1` so a stray `localplaud run` isn't exposed to the LAN.
  Docker overrides it to `0.0.0.0` (the container sits behind Caddy and its port
  isn't published). `api.auth_token`, when set, requires an `X-Auth-Token`
  header or `?token=` on every request except `/healthz`. For real deployments
  the recommended front line is Caddy `basic_auth`.
- **Fetches are SSRF-guarded.** URLs pulled from API responses must be `https`
  and must not resolve to private/loopback/link-local/reserved IPs; redirects
  are not followed after the check. This blocks a compromised or MITM'd response
  from steering the client at cloud-metadata or internal services.
- **Downloads are bounded.** Audio is capped (2 GiB) and gzip assets use a
  size-bounded decompress (128 MiB) to defend against decompression bombs.
- **Cloud ids are validated before use in filesystem paths**
  (`^[A-Za-z0-9_-]{1,128}$`) to prevent path traversal.
- **Untrusted text is escaped** before the client-side markdown pass in the UI
  (Jinja autoescape plus an explicit HTML-escape in the summary renderer).
- **Secrets never touch git or the image.** Tokens/keys live in `.env` or the
  environment; `.gitignore`/`.dockerignore` exclude `.env*`, `config.toml`,
  `*.cookie`, `*.token`, `secrets/`, and `data/`. Nothing logs secret values.

## Consequences

- The default local experience is safe, but exposing the UI to a network still
  requires the operator to set `auth_token` and/or put auth in the reverse proxy
  — documented in the README and deploy guide.
- The SSRF allowlist is deny-private rather than allow-listed hosts, chosen
  because the API host is region-variable and user-supplied; a stricter host
  allowlist can be layered on later if needed.
