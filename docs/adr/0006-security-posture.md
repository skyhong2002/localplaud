# 6. Security posture

Status: Accepted

## Context

localplaud handles a user's private recordings, authenticates to the Plaud
cloud through official OAuth, downloads bytes from URLs found in API responses,
and serves a web UI. An independent review surfaced concrete risks worth
addressing before wider use.

## Decision

- **Web UI is loopback-by-default; remote deployments use individual accounts.**
  `api.accounts_enabled` enables Google OIDC and local passwords, pending approval,
  a protected owner and member accounts, one private workspace per account
  ([ADR 0008](0008-workspace-isolation.md)) and user-bound revocable sessions. See
  [accounts](../accounts.md) for bootstrap, recovery and workspace boundaries. Account mode rejects legacy shared passwords and API tokens. Opaque
  session cookies remain HttpOnly, Secure and SameSite=Lax; only peppered hashes
  are stored. Unsafe browser requests require the trusted same origin. Worker
  authentication remains separate. `/healthz` and explicitly issued public share
  links remain public. Legacy shared login is available only with accounts disabled.
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

- The default local experience is safe, but exposing the UI to a network requires
  HTTPS plus the built-in login credentials (or an independently reviewed upstream
  authentication layer) — documented in the README and deploy guide.
- The SSRF allowlist is deny-private rather than allow-listed hosts, chosen
  because the API host is region-variable and user-supplied; a stricter host
  allowlist can be layered on later if needed.
