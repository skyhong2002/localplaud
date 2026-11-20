# localplaud on Android

The Android APK opens the existing mobile workspace as a Trusted Web Activity.
It uses the installed browser for sign-in, playback, uploads, and downloads. Your
server still owns the recordings, processing jobs, and AI configuration. No Plaud
credentials or recording data are bundled in the APK. Long-press the localplaud
launcher icon for Sync Plaud, All files, Search, and Explore.

The app inherits the deployed workspace's capabilities and limitations: files,
transcript and speaker edits, notes, mind maps, Ask, organization, templates,
AutoFlow, and exports use the same routes and permissions as the web app. It does
not pair with recorders, replace Plaud device sync, run AI on the phone, or keep
the library available offline. The official Plaud App remains the path for raw
audio upload from a Plaud recorder.

In a recording's **Sources** tab, expand the playback controls for a seekable
waveform, 15-second skips, and playback speed. The inline and bottom controls
use the same audio; switching between Sources and Notes does not restart it.

The library has quick links for **All files**, **Recordings**, and **Imports**.
Recordings shows known recorder and app captures; Imports shows known file
uploads. Unclassified older items remain in All files. Opening a recording and
going back preserves the selected view and filters. Use **Share & export** to
find the recording's download and sharing options in one place.

In **All files**, select up to 50 recordings and choose **Generate notes** to
preview eligibility. Confirmation queues derived work without rerunning speech
recognition or sending anything to Plaud. The worker uses each recording's
automatic template and profile. If your server limits automatic processing to
untranscribed recordings, older local transcripts may be skipped; open those
recordings to generate notes individually.

## Sync a Plaud recorder

1. Long-press the **localplaud** icon and tap **Sync Plaud** to open the installed
   official Plaud app.
2. Sync the recorder in Plaud as usual and wait for the raw audio to upload.
   There is no need to select **Generate** in Plaud.
3. Open **localplaud** (or your usual browser bookmark) and check the library or
   system status. With official OAuth and automatic raw-audio processing enabled,
   the server picks up the recording on a later poll and processes it without
   keeping either phone app open. The shortcut itself does not upload, run a poll,
   trigger generation, or pass credentials between apps.

The HTTPS debug APK installed on Pixel 7 showed **Sync Plaud** in the launcher
menu. A follow-up confirmed both apps were installed but could not exercise the
cross-app launch because another device session held the phone. The
recorder-to-workspace path remains unverified. The production association
endpoint was still not configured, so the APK is not a verified standalone TWA.

### Verify your daily sync workflow

Use a new, short recording with non-sensitive content. Confirm the recorder
has uploaded it in the official Plaud app, without selecting **Generate**.
Wait for the next server poll (`[poller].interval_seconds`, 300 seconds by
default), then find the recording in **All files**. Processing can take longer
than a poll interval; follow the recording's stage state or open **Settings →
System health**. Once ready, check playback, the editable speaker-labelled
transcript, local notes, and export. This check does not require Ask or AutoFlow.

- **No new file:** first confirm upload in Plaud, then check server OAuth and
  poller health. Opening either phone app does not force a server poll.
- **File present, no transcript or notes:** check the recording's stage state.
  A successful poll is not the same as a successful download or processing run.
  Check automatic audio download and processing eligibility before trying a
  second upload or manually resuming the job.
- **Browser controls still visible:** check Android origin verification below.
  This does not prevent the server from discovering or processing uploads.

Do not count a synthetic browser run, a successful shortcut launch, or an
anonymous health check as a pass for this recorder-to-workspace test.

To record in localplaud, choose **Home → New recording → Record audio** on
desktop or mobile, or **+ → Record audio** on the phone. Give the browser
microphone permission, then pause or resume as needed, stop, and preview the clip.
New browser recordings appear under **App recording** in the library's source
filter. File uploads use **Imported audio**. Existing rows are not relabelled.
Only **Save recording**
uploads it to the configured workspace; **Discard recording** sends nothing.
After stopping, select **Keep draft on this device** to recover the recording
from **Add audio** after a refresh or relaunch. Drafts never upload by themselves.
The browser keeps up to three per account/workspace. They expire after seven days
and are removed when localplaud next checks storage, or after a successful Save.
localplaud does not encrypt this storage: avoid keeping sensitive drafts in a
shared browser profile. This is foreground capture, up to one hour or
approximately 128 MiB, whichever comes first. It does not keep recording in the
background or recover a recording interrupted before Stop.

## Build and install

With JDK 17 or newer, Gradle 8.11.1, and Android SDK Platform 36:

```sh
gradle -p android -PlocalplaudOrigin=https://plaud.example.com assembleDebug lintDebug testDebugUnitTest
adb install -r android/build/outputs/apk/debug/localplaud-android-debug.apk
```

Set `localplaudOrigin` to the final HTTPS origin where you normally sign in, not
an alias that redirects elsewhere. Use a browser with Trusted Web Activity support
for the standalone experience. For distribution, run `assembleRelease` against
the same HTTPS origin and sign the release APK with your own key. Use the same
signing key for updates; debug and release APKs are not interchangeable updates.

## Verify the workspace origin

Read the SHA-256 fingerprint of the certificate that signed the installed APK
using `apksigner verify --print-certs`. This is not the fingerprint of your HTTPS
certificate. Set `[api].android_signing_fingerprints` in your `config.toml` to a
list of those digests (raw hexadecimal or colon-separated), or set
`LOCALPLAUD_API__ANDROID_SIGNING_FINGERPRINTS` to the same list as a JSON array.
The server publishes only those public digests at `/.well-known/assetlinks.json`.
The default empty list leaves the endpoint disabled.

If your reverse proxy requires its own authentication, exempt only this exact
GET route or serve the following statement directly at the proxy. Check that the
final HTTPS origin serves it as `application/json` without a redirect or login:

```json
[
  {
    "relation": ["delegate_permission/common.handle_all_urls"],
    "target": {
      "namespace": "android_app",
      "package_name": "org.localplaud.android",
      "sha256_cert_fingerprints": ["REPLACE_WITH_SIGNING_CERT_SHA256"]
    }
  }
]
```

Do not add a signing fingerprint to a deployment you don't administer. Until the browser verifies the
association, it keeps browser controls visible. Navigation to other origins, such
as Google sign-in, can also show browser controls intentionally.

For an isolated local smoke test, use an existing synthetic workspace on port
18771, run `adb reverse tcp:18771 tcp:18771`, and build debug with
`-PlocalplaudOrigin=http://127.0.0.1:18771`. This debug-only origin does not
exercise HTTPS verification. After testing, remove the port forward with
`adb reverse --remove tcp:18771`.

The debug APK was installed and launched on a USB-connected Pixel 7 running
Android 16 with a synthetic workspace. Home, recording detail, and the recent
speaker-name sheet loaded in a browser custom tab. Calendar date filtering,
opening the local note-generation form without submitting it, and recording
with the phone microphone and discarding the clip were also checked. This does
not validate production sign-in, standalone Trusted Web Activity verification,
or the full processing workflow on the phone. If the Android association
endpoint returns `401` to an unauthenticated request, fix that endpoint's deployment access
before treating the workspace as eligible for standalone mode.

If you use `scripts/serve_demo.sh` from a deployment checkout, set
`PYTHON_DOTENV_DISABLED=1` so the demo does not load deployment settings from the
checkout's `.env`. See [Android workflow parity](android-parity.md) for the
interaction backlog and verification status.
