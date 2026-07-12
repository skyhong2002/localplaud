# Plaud cloud API notes

localplaud only ever issues **read-only** requests to the Plaud cloud. Two
providers exist; this document is the reference both clients are built from.

## Product boundary

The official API is used for OAuth, recording discovery, minimal metadata, and raw
audio download. The subscription-independent pipeline must succeed when
`source_list`, `note_list`, and every Plaud AI artifact are empty.

Plaud transcripts/summaries may be imported only through an explicit migration or
debug path. They must retain Plaud provenance and must not silently satisfy local
pipeline completion. See [ADR 0007](adr/0007-subscription-independence.md).

## Official Open API (default provider — `plaud/official.py`)

Sanctioned developer API, documented at <https://docs.plaud.ai> (MCP & CLI).
All endpoints verified against a real account (2026-07-10).

### Official MCP provider

Set `plaud.provider = "mcp"` after running
`npx -y @plaud-ai/mcp@latest install`. localplaud starts the official MCP stdio
server and uses only its read tools (`get_current_user`, `list_files`, `get_file`,
`get_note`, and `get_transcript`). Listing and `presigned_url` raw-audio download
are valid primary ingest inputs. MCP notes/transcripts remain explicitly labelled
migration/debug inputs and cannot satisfy independent-mode processing.

- **Base**: `https://platform.plaud.ai/developer/api`, auth `Authorization:
  Bearer <access_token>`.
- **OAuth** (PKCE, authorization-code):
  - Authorize (browser): `https://web.plaud.ai/platform/oauth?client_id=…&redirect_uri=…&response_type=code&code_challenge=…&code_challenge_method=S256&state=…`
  - Exchange: `POST /oauth/third-party/access-token` (form-encoded `code`,
    `redirect_uri`, `code_verifier`, `state`; `Authorization: Basic
    base64(client_id:)` — the official CLI's client is public, no secret).
  - **Refresh**: `POST /oauth/third-party/access-token/refresh` with just
    `refresh_token` (form-encoded). Response rotates both tokens; a missing
    `refresh_token` in the response means "keep the old one".
  - Token cache: `~/.plaud/tokens.json` — `{access_token, refresh_token,
    token_type, expires_at}` (epoch **ms**; access token lives 24 h). Written
    atomically with mode `0600` by native `localplaud auth login`, compatible
    with the official CLI, and refreshed by localplaud.
- **Endpoints** (all GET):
  - `/open/third-party/users/current` — whoami (`id`, `email`, `nickname`).
  - `/open/third-party/files/?page=&page_size=` — listing, `{type, data:
    [{id, name, created_at, start_at, duration, serial_number}], page,
    page_size}`. `page_size` 10–100, `page` ≤ 1000; a short page ends the
    walk. Timestamps are naive ISO strings in UTC; `duration` is a string of
    milliseconds. **File ids are identical to the web API's** — the two
    providers merge cleanly.
  - `/open/third-party/files/{id}` — the listing fields plus:
    - `presigned_url` — 24 h S3 audio URL (`.mp3`).
    - `source_list[]` — optional Plaud-generated transcript assets; the
      `data_type == "transaction"`
      entry's `data_content` is a JSON **string** of segments `{content,
      start_time, end_time, speaker, original_speaker, embeddingKey}` (times
      in ms; `speaker` reflects user renames, `original_speaker` is
      "Speaker N"). Other types seen: `outline`, `transaction_polish`.
    - `note_list[]` — optional Plaud-generated artifacts; the
      `data_type == "auto_sum_note"` entry's `data_content` is Plaud's summary as
      markdown. Neither list is a primary-pipeline dependency.
- **Not exposed** (hence the optional apse1 enrichment): `version`,
  `file_md5`, `edit_time`, `is_trash`, tags, scene.

## Deprecated legacy web API (`plaud/client.py` — reverse-engineered)

This adapter is retained only so existing installations can migrate. New
deployments must use the official Open API or official Plaud MCP. It receives no
new product features and is excluded from current acceptance paths.

Everything below was observed **read-only**, against the account owner's own
data, from the browser at <https://web.plaud.ai>. Anything marked **open** is
not yet confirmed.

## Hosts

| Purpose      | Host                                         |
| ------------ | -------------------------------------------- |
| Web app      | `https://web.plaud.ai` (Vue 3 SPA)           |
| API          | `https://api-apse1.plaud.ai` (region-specific) |
| Static       | `https://web-static.plaud.ai`                |

> The API host is **per-account / per-region**. The browser stores it in
> `localStorage["pld_plaud_user_api_domain"]`. Read it from your own browser
> and set `plaud.api_base` — do not hardcode `apse1`.

## Authentication

Plaud uses **header-token auth, not a readable cookie.** Findings:

- `document.cookie` only exposes analytics / load-balancer cookies
  (`_ga`, `AWSALBTG`, `cookieyes-consent`, …) — no reusable session token.
- The `pld_*` `localStorage` keys are UI/workspace state, not credentials.
- Authenticated XHRs to `api-*.plaud.ai` (e.g. `GET /user/me` → 200) send an
  `Authorization` header plus a set of Plaud client/device headers. The API's
  CORS policy allow-lists exactly this vocabulary:

  ```
  Authorization, Content-Type, X-Request-ID, x-device-id, timezone,
  app-language, app-platform, app-version, app-versionNumber, edit-from,
  x-pld-user, X-Encrypt-Response
  ```

### What a headless client must send

At minimum an `Authorization` value plus the Plaud client/device headers that
your account's requests carry (notably `x-device-id` and `x-pld-user`).
localplaud validates the set with `GET /user/me`.

**Supported route today (v1):** copy an authenticated request out of the
browser and let localplaud replay it. The easiest way:

```
DevTools → Network → click any api-*.plaud.ai request →
Copy → Copy as cURL   →   pipe into:  localplaud auth import
```

`localplaud auth import` parses the cURL and prints the `.env` lines to set
(`LOCALPLAUD_PLAUD__API_BASE`, `LOCALPLAUD_PLAUD__COOKIE` for the
`Authorization` value, and `LOCALPLAUD_PLAUD__EXTRA_HEADERS` for the device
headers). See [ADR 0002](adr/0002-plaud-auth-strategy.md).

**Open:** the exact `Authorization` scheme, which individual headers are
strictly required, and the programmatic login flow (email/OTP →
token, likely involving `pld_pubKey` / `pld_passAlgorithm` client-side
signing). Until that's reverse-engineered, sessions are pasted and will need
re-pasting when they expire.

## Endpoints (all GET)

### `GET /user/me`
Auth validation. 200 when the header set is valid.

### `GET /file/simple/web`
The file list. Query params:

| param      | meaning                                        |
| ---------- | ---------------------------------------------- |
| `skip`     | pagination offset                              |
| `limit`    | page size                                      |
| `is_trash` | `0` = normal, `2` = include trash              |
| `sort_by`  | `start_time` \| `edit_time`                    |
| `is_desc`  | `true` \| `false`                              |

Response:

```json
{ "status": 0, "msg": "success", "data_file_total": N,
  "data_file_list": [ { ...file... } ] }
```

File object (fields localplaud syncs): `id` (primary key), `filename`,
`fullname` (`<id>.opus`), `filesize`, `file_md5`, `duration` (ms),
`start_time`/`end_time` (epoch ms), `scene`, `is_trash`,
`version`/`version_ms` (change detection), `edit_time`, `is_trans`,
`is_summary`. The full raw object is stored on `PlaudFile.raw`.

### `GET /file/detail/{file_id}`
**Confirmed:** the SPA renders both the timestamped, speaker-labelled
**transcript** and the template **summary / notes** (observed template name
`Adaptive Summary`, with section headings and action items) from this single
payload. There is no separate transcript or summary endpoint — probes of
`/trans/{id}`, `/ai/summary/{id}`, `/file/{id}/summary` all 404.

**Open:** the exact JSON key layout and the transcript segment schema (how
speakers / word timestamps are represented).

### `GET /file/temp-url/{file_id}` — audio download ✅
**Confirmed.** Clicking play/download on a recording calls this with the bare
file id (not `fullname`). It returns a small JSON wrapper around a **signed,
expiring AWS S3 URL**:

```
https://apse1-prod-plaud-bucket.s3.amazonaws.com/audiofiles/{file_id}.mp3
    ?AWSAccessKeyId=…&Signature=…&x-amz-security-token=…&Expires=…
```

The real asset is **MP3**, despite the list metadata's `<id>.opus` `fullname`
convention — so take the extension from the URL, not `fullname`, and treat the
URL as short-lived. `PlaudClient.get_temp_url` / `download_audio` implement
this (scanning the wrapper for the signed URL, since its exact JSON key wasn't
extractable). **Open:** the exact wrapper key name.

### Cloud transcript / summary assets (migration/debug only)
The `/file/detail/{id}` payload resolves to signed S3 assets on
`apse1-prod-plaud-content-storage.s3.amazonaws.com`:

| Asset | Path pattern |
| --- | --- |
| Transcript | `.../file_transcript/{id}/trans_result.json.gz` |
| Summary (markdown) | `.../file_summary/{id}/ai_content.md.gz` |
| Outline | `.../file_outline/{id}/outline.json.gz` |

`PlaudClient.get_cloud_summary_md` and `get_cloud_transcript_json` can fetch and
gunzip these for migration, debugging, or benchmark comparison. Imported results
must remain labelled `source=plaud`; the primary pipeline derives its own artifacts
from downloaded audio.

## Open questions (tracked in issue #1)

1. Exact `Authorization` scheme/value and which custom headers are mandatory.
2. httpOnly cookie names/domains/expiry (if any are load-bearing).
3. Login endpoint + `pld_pubKey` / `pld_passAlgorithm` derivation.
4. Exact JSON keys of `/file/detail/{id}` and the transcript segment schema.
5. The signed audio-download URL endpoint, response body, and CDN host pattern.
