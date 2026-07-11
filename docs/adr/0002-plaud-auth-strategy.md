# ADR 0002: Plaud cloud authentication — replay a pasted browser session

Status: Superseded for primary auth; retained for optional legacy enrichment

The default provider now uses Plaud's sanctioned read-only Open API and native
loopback S256 PKCE (`localplaud auth login`). The pasted browser-session strategy
below remains available only for optional api-apse1 enrichment fields that the Open
API does not expose; it is not required for ordinary setup or raw-audio processing.

## Context

localplaud needs an authenticated, headless session against the user's own
Plaud cloud account (`api-apse1.plaud.ai` or the region host from
`pld_plaud_user_api_domain`). Historical reverse-engineering findings (see
`AGENTS.md` and `docs/plaud-api.md`):

- Auth is **header-token based, not a readable cookie**. `document.cookie`
  only contains analytics/ALB cookies; nothing there is a reusable session
  token, and `localStorage`'s `pld_*` keys are UI/workspace state.
- CORS on the API allows a specific custom-header set: `Authorization`,
  `X-Request-ID`, `x-device-id`, `timezone`, `app-language`,
  `app-platform`, `app-version`, `app-versionNumber`, `edit-from`,
  `x-pld-user`, `X-Encrypt-Response`, etc. An authenticated
  `GET /user/me` returns 200.
- The login flow (email/OTP, and whatever signing `pld_pubKey` /
  `pld_passAlgorithm` imply) has **not** been reverse-engineered. The
  exact `Authorization` scheme and the minimal mandatory header subset are
  also unconfirmed.

## Decision

For v1, the user captures the header set of an authenticated XHR from
browser DevTools (at minimum `Authorization` plus the Plaud client/device
headers) and pastes it into localplaud (env var / config; `extra_headers`
holds the client headers). localplaud stores this and replays it on every
request, validating the session with `GET /user/me` at startup and before
each poll cycle.

Programmatic login (email/OTP exchange, `pld_pubKey`/`pld_passAlgorithm`
signing) is a documented **TODO**, to be attempted only after the paste
flow proves the rest of the pipeline.

## Consequences

- Works today with zero un-reversed protocol; strictly read-only GETs
  against the user's own account.
- **Sessions can expire**, and we don't know the token lifetime. When
  `/user/me` starts failing, localplaud must surface a clear
  "re-paste your session" error (CLI and UI) rather than silently stalling
  the poller. Users should expect occasional re-pastes.
- Onboarding requires DevTools comfort; the README must document the
  capture step precisely.
- If Plaud changes the header contract, only the client module changes;
  the pasted-blob approach (store whole header set, replay verbatim) is
  deliberately tolerant of headers we don't understand yet.
