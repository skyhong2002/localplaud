# Completion notifications through Hermes

`scripts/localplaud_done_watch.py` is the deployed script-only Hermes cron watcher.
The scheduler runs it every five minutes with `no_agent: true`; no language model
is involved in checking or formatting completion notifications. Its stdout is the
message to deliver. An empty stdout is a silent successful run.

Configuration uses `LOCALPLAUD_URL` (default `https://plaud.observe.tw`),
`LOCALPLAUD_TOKEN_FILE`, and `LOCALPLAUD_WATCH_STATE`. Token/state paths default to
`localplaud_api_token` and `cron/state/localplaud_done_watch.json` under
`HERMES_HOME` (or `~/.hermes`). Keep token and state files outside the repository.

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
