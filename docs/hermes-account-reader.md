# Hermes account-based recording tools

The completion notification feed only exposes completion metadata. Reading
transcripts and notes uses the normal local account login and revocable browser
session; no Google authorization or notification token is involved.

## Install in an existing Hermes MCP integration

Place `scripts/localplaud_ro.py` at the existing adapter path and place
`scripts/localplaud_read.py` beside it. Keep the existing MCP server configuration,
command and Python interpreter. The adapter uses the existing `FastMCP` API from
MCP v1 (`mcp<2`); do not upgrade that runtime to MCP v2 as part of this deployment.
Restart or reconnect the actual MCP process after replacing its files.

Create a private JSON file owned by the Hermes service user, mode `0600`, containing
`base_url`, `username`, and `password`. Use the HTTPS site origin and an existing
approved account with a local password. Set `LOCALPLAUD_ACCOUNT_FILE` to its path,
or use the default `~/.hermes/localplaud_account.json`. Never put credentials in
chat, command-line arguments, repository files or model-facing configuration.

A private `.cookies` file and `.lock` file are created beside the credential file.
Requests reuse the session. On HTTP 401 the client logs in once with the local
password, replaces the session atomically, and retries. HTTP 403 is reported as a
permission/approval failure; it does not loop logins. Requests and login redirects
cannot leave the configured site origin. Concurrent MCP calls share a lock.

The adapter exposes read operations only, but the stored credentials retain the
account's actual server-side permissions. Changing the account password requires
updating this private JSON file. Never change the completion notification token,
its server digest, cron configuration or notification baseline during this migration.

## Compatible tools and complete text

The server remains `localplaud-readonly`. Existing tool names remain available:

- `localplaud_diagnostics`: redacted runtime diagnostics.
- `localplaud_list_recordings`: bounded listing with the original filters.
- `localplaud_recording_page`: canonical transcript, notes and metadata as Markdown.
- `localplaud_recording_usage`: processing usage and cost ledger.

Two additional tools read individual content types:
`localplaud_recording_transcript` and `localplaud_recording_notes`.
The content tools read actual text exports, not a progressive-loading HTML shell.

The original `file_id` and `max_chars` parameters are preserved. Content tools also
accept `offset` (default 0). Each result reports `total_chars` and either
`next_offset` or `end_of_document=true`. Follow every continuation before treating
a long recording as fully read. No transcript tail is silently discarded.

## Verification

Use `python localplaud_read.py --credentials /private/account.json check` for an
aggregate connectivity check. This is not sufficient proof of content access.
Through the loaded MCP adapter, call the transcript and notes tools for a known
recording and follow all continuations. Compare the complete result with canonical
exports without writing private recording text into diagnostic logs. Verify an
expired/revoked adapter session reauthenticates and persists its replacement.

The standalone helper also accepts `list`, `transcript ID`, `notes ID` and
`recording ID`. It shares the adapter's authentication code; installing this helper
alone does not update tools in an already-running Hermes MCP process.

Executable regressions:

```sh
uv run --no-project --with 'mcp<2' --with pytest python -m pytest \
  tests/test_account_reader.py tests/test_mcp_account_reader.py -q
```
