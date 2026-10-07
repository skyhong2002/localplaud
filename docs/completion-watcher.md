# Completion notifications through Hermes

`scripts/localplaud_done_watch.py` is the deployed script-only Hermes cron watcher.
The scheduler runs it every five minutes with `no_agent: true`; no language model
is involved in checking or formatting completion notifications. Its stdout is the
message to deliver. An empty stdout is a silent successful run.

Configuration uses `LOCALPLAUD_URL` (default `https://plaud.observe.tw`),
`LOCALPLAUD_TOKEN_FILE`, and `LOCALPLAUD_WATCH_STATE`. Token/state paths default to
`localplaud_api_token` and `cron/state/localplaud_done_watch.json` under
`HERMES_HOME` (or `~/.hermes`). Keep token and state files outside the repository.

## Credential and endpoint

The watcher reads `GET /api/integrations/completion-status` using an
`Authorization: Bearer` header. This dedicated machine credential works independently
of Google/browser sessions and legacy authentication. The endpoint returns only
non-trash recordings' IDs, display titles, processing statuses, start times, and
durations. It cannot read transcripts, summaries, audio or settings, or mutate data.
Responses are never cached. Without configuration the endpoint rejects all requests.

Generate a new random token (at least 32 random bytes) and store it in the watcher's
token file with mode `0600`. Put its lowercase SHA-256 hex digest in the server's
private environment as `LOCALPLAUD_API__COMPLETION_TOKEN_SHA256`, then restart the
server. Only the digest is needed on the server. The token must be distinct from
the retired shared API key and all browser/worker credentials. To revoke or rotate
it, remove or replace the digest and restart; update the watcher's file to match.

When upgrading an existing watcher, replace its script and token file but retain
the configured state path and contents. Account mode deliberately rejects the old
`X-Auth-Token` on `/api/files`; leaving the old watcher in place produces HTTP 401.
Update both ends before checking recovery. Do not reset the completion baseline.

## Delivery and recovery

A first successful poll establishes the baseline without announcing historical
recordings. Later successful polls report transitions to `done`. The existing
status-state format remains compatible. An advisory lock serializes overlapping
runs, and state files are replaced atomically.

A request has a ten-second socket timeout. Transient network failures and HTTP
408/429/500/502/503/504 retry once after one second. Failed polls never replace
the completion baseline. The first two failed polling runs stay silent; the third
produces a source-specific warning. Continued failures warn at most once per hour.
Authentication, malformed API data and unreadable state warn immediately, with the
same hourly limit. A successful poll resets the outage counter and resumes normal
completion detection, including recordings completed during the outage.

The adjacent `.health.json` file retains consecutive failure count, last failure
and alert times, a sanitized error, or the last successful poll time. Transient
failures intentionally return success with empty stdout so Hermes does not label
an API interruption as a model-provider or script failure. Unexpected script bugs
still fail normally. Notification transport/acknowledgement belongs to Hermes;
this polling state is not an end-to-end delivery receipt.

Run `python scripts/localplaud_done_watch.py --check` with the configured token to
verify API access without printing recording content, sending a notification, or
advancing any state. Test outage/recovery and completion output using an isolated
state path; never advance production state just to test a notification.

The September 23 incident included HTTP 502 responses during the application's
model-migration restart. The earlier watcher raised on every failed request,
turning a short maintenance interruption into repeated generic cron alerts.
