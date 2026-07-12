# Private workspace backups

Settings → **Data & backup** creates a consistent archive without stopping
localplaud. The default archive contains an online SQLite snapshot. The optional
full archive also contains regular files under the configured media directory.

Every ZIP contains:

- `manifest.json` using `localplaud-workspace-backup/v1`;
- `database/localplaud.db`, copied through SQLite's online backup API;
- optional `media/…` files; and
- an external `.zip.sha256` sidecar retained on the host. The Settings catalog
  and API expose the same digest for the downloaded file.

The archive deliberately excludes `.env`, process environment variables,
`config.toml`, Plaud OAuth token files, reverse-proxy credentials, and provider
secret values. Database rows may contain opaque references such as
`env:OPENAI_API_KEY`, but not the referenced value. Transcripts and notes are
sensitive user data, so the resulting ZIP must still be stored privately.

## Verify a download

Compare the downloaded file's SHA-256 with the full digest shown in Settings or
returned by `GET /api/backups`. Then inspect `manifest.json` before restoring:

```bash
shasum -a 256 localplaud-*.zip
unzip -p localplaud-*.zip manifest.json
```

## Restore

Restoring is intentionally offline; a live Web request must never replace the
database underneath active workers.

1. Stop localplaud, the poller, and workers.
2. Verify the archive digest and manifest as above.
3. Make another copy of the current database and media directory.
4. Extract `database/localplaud.db` and replace the file referenced by
   `store.database_url`.
5. For a full backup, copy the contents of `media/` into the configured
   `poller.download_dir`. Preserve ownership expected by the service account.
6. Start localplaud and open Status. Startup runs idempotent schema migrations;
   confirm the database health, recording count, and stage queue before deleting
   the pre-restore copy.

Do not restore `.env`, tokens, or provider credentials from an untrusted source.
They are intentionally managed separately from workspace content.
