# Private workspace backups

Settings → **Data & backup** creates a consistent archive without stopping
localplaud. The default archive contains an online SQLite snapshot. The optional
full archive contains the media referenced by that database snapshot, plus regular
files (including note assets and playback caches) in its recording directories.
Directories for uploads absent from the snapshot and hidden staging files are
excluded.

Every ZIP contains:

- `manifest.json` using `localplaud-workspace-backup/v1`;
- `database/localplaud.db`, copied through SQLite's online backup API;
- optional `media/…` files; and
- an external `.zip.sha256` sidecar retained on the host. The Settings catalog
  and API expose the same digest for the downloaded file.

Full backups add `media.entries` (archive path, byte count, SHA-256 per file) and
`media.references` (recording ID, database path column, archive path) to the v1
manifest. `media.source_paths` records the source media root, working directory,
and any home-prefix expansions used by the snapshot's paths. Restore derives each
recording/column's expected archive member from its database path and that context;
swapping references between existing audio files is rejected. Counts and bytes
describe the files actually archived. A short SQLite
write barrier covers the online snapshot, inventory, and opening of media file
descriptors. Existing upload publication and eviction serialize through database
writes, so they cannot change those references during capture. The barrier is
released before copying the media. Open descriptors preserve captured bytes through
later atomic replacement or eviction; a missing reference, unsafe symlink, or
detected in-place change aborts the backup without publishing a partial archive.
This does not coordinate arbitrary filesystem writers that bypass the application's
database publication protocol.

Media paths must use the configured root's canonical spelling. In particular,
`/var/…` database paths with a `/private/var/…` resolved media root on macOS are
currently rejected. This check intentionally does not resolve referenced paths:
symlinks in media path components remain forbidden.

Capture uses one open descriptor per media file. A library larger than the process's
descriptor allowance fails safely; increase the service's descriptor allowance
before retrying. A busy database can also require a retry. Full backups need enough
disk space for the archive plus its temporary database snapshot. New archives,
snapshots, and restore files use owner-only permissions. Existing backup directories
retain their operator-managed permissions.

The archive deliberately excludes `.env`, process environment variables,
`config.toml`, Plaud OAuth token files, reverse-proxy credentials, and provider
secret values. Database rows may contain opaque references such as
`env:OPENAI_API_KEY`, but not the referenced value. Transcripts and notes are
sensitive user data, so the resulting ZIP must still be stored privately.

## Authorized cross-host upload

Settings can upload a completed archive to an explicitly authorized private host.
Each destination stores a display name, base URL, optional `env:VARIABLE` bearer-token
reference, enabled state, and whether private/LAN addressing is allowed. The secret
value stays in the process environment and is never copied into the database,
archive, UI, or delivery history.

Public destinations require HTTPS. HTTP and private, loopback, link-local, or other
non-routable addresses are accepted only when **Allow private/LAN addresses** is
selected. URLs containing inline credentials, query parameters, or fragments are
rejected. Redirects are not followed, so a configured host cannot redirect an
archive to an unapproved destination.

**Test** sends an authenticated `OPTIONS` request without recording or archive data.
**Upload** sends the ZIP with HTTP `PUT` to:

```text
<destination base URL>/<percent-encoded archive filename>
```

The request includes `Content-Type: application/zip`, the exact content length,
`X-Localplaud-Backup-Sha256`, and a stable `X-Localplaud-Delivery-Id`. A configured
token is sent as `Authorization: Bearer …`. The receiver must return a 2xx status.
Failures are durable and independently retryable; retrying uses the same delivery ID
and archive bytes. A completed archive/destination pair is idempotent and is not sent
again. Revoking a destination preserves non-secret history but prevents future retry.

## Verify a download

Compare the downloaded file's SHA-256 with the full digest shown in Settings or
returned by `GET /api/backups`. Then inspect `manifest.json` before restoring:

```bash
shasum -a 256 localplaud-*.zip
unzip -p localplaud-*.zip manifest.json
```

## Restore

### Isolated verification drill

Use a newly created full backup to verify a restore without touching the active
database, media directory, settings, or workers. Supply the full archive SHA-256
recorded separately from the download and a destination that does not exist:

```python
from pathlib import Path
from localplaud.backups import restore_workspace_backup

restored = restore_workspace_backup(
    Path("/private/backups/localplaud-20261018T000000Z-12345678.zip"),
    Path("/private/restore-drill"),
    expected_sha256="<full trusted SHA-256>",
)
print(restored)  # database and media_root paths for the isolated copy
```

The helper pins one archive file descriptor for hashing, manifest reads, and
extraction. It rechecks the complete digest and file size, mtime, and ctime before
returning success; replacement or in-place modification cannot substitute another
archive after the trusted digest check. This adds a second full archive read.
Any detected change removes the isolated restore.

The helper verifies the archive digest, inventory, member sizes and checksums,
database integrity, and complete media-reference mapping. It rejects unsafe member
paths and existing destinations, removes a failed restore, and rewrites the restored
`audio_path`/`wav_path` columns to files inside the new destination. It never loads
live settings or starts workers. The restored database differs from the archived
database checksum after this intentional path rewrite. Do not start automatic
processing or external integrations against a drill copy. Older v1 archives without
per-file checksums or source path context, and database-only archives, require the
manual procedure below.

### Offline replacement

Restoring is intentionally offline; a live Web request must never replace the
database underneath active workers.

1. Stop localplaud, the poller, and workers.
2. Verify the archive digest and manifest as above.
3. Make another copy of the current database and media directory.
4. Extract `database/localplaud.db` and replace the file referenced by
   `store.database_url`.
5. For a full backup, copy the contents of `media/` into the configured
   `poller.download_dir`. Preserve ownership expected by the service account. If
   that directory differs from the original installation, remap the database's
   `audio_path`/`wav_path` values before starting; restoring the database alone does
   not make its original absolute paths portable. Prefer the verified isolated
   helper above for a full backup with the new inventory.
6. Start localplaud and open Status. Startup runs idempotent schema migrations;
   confirm the database health, recording count, and stage queue before deleting
   the pre-restore copy.

Do not restore `.env`, tokens, or provider credentials from an untrusted source.
They are intentionally managed separately from workspace content.
