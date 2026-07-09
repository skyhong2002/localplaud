# Plaud cloud API — reverse-engineering notes

Everything here was observed **read-only**, against the account owner's own
data, from the browser at <https://web.plaud.ai>. localplaud only ever issues
GET requests to the Plaud cloud. This document is the reference the
`localplaud.plaud` client is built from; anything marked **open** is not yet
confirmed.

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

### Cloud transcript / summary assets ✅ (bonus)
The `/file/detail/{id}` payload resolves to signed S3 assets on
`apse1-prod-plaud-content-storage.s3.amazonaws.com`:

| Asset | Path pattern |
| --- | --- |
| Transcript | `.../file_transcript/{id}/trans_result.json.gz` |
| Summary (markdown) | `.../file_summary/{id}/ai_content.md.gz` |
| Outline | `.../file_outline/{id}/outline.json.gz` |

`PlaudClient.get_cloud_summary_md` and `get_cloud_transcript_json` fetch and
gunzip these (best-effort, by URL substring). The summary is plain markdown and
directly usable; the transcript JSON schema isn't modelled yet (issue #9).

## Open questions (tracked in issue #1)

1. Exact `Authorization` scheme/value and which custom headers are mandatory.
2. httpOnly cookie names/domains/expiry (if any are load-bearing).
3. Login endpoint + `pld_pubKey` / `pld_passAlgorithm` derivation.
4. Exact JSON keys of `/file/detail/{id}` and the transcript segment schema.
5. The signed audio-download URL endpoint, response body, and CDN host pattern.
