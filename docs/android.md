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

1. Long-press the **localplaud** icon, tap **Sync Plaud**, then choose **Open
   official Plaud app**.
2. Sync the recorder in Plaud as usual and wait for the raw audio to upload.
   There is no need to select **Generate** in Plaud.
3. Open **localplaud** (or your usual browser bookmark). Use **Home → Check
   Plaud now → Check now** to look for the upload immediately, or let the server
   find it automatically. A successful manual check links to the newly found
   recording or the library. "Queued for audio download" does not mean the
   download or processing has finished. Both follow the server settings and
   continue without keeping either phone app open. The launcher shortcut itself
   does not upload, run a poll, trigger generation, or pass credentials between apps.

### Check a Plaud Note connection

The **Sync Plaud** shortcut also offers foreground USB and Bluetooth diagnostics.
The USB check looks for a mass-storage interface without opening the device.
[Plaud's USB documentation](https://support.plaud.ai/hc/en-us/articles/51990314311449-Laptop-doesn-t-recognize-Plaud-Note)
says Note firmware 2.1 and later disables USB file access; on older firmware,
check the recorder's **Access via USB** setting and use the original cable.
An interface alone does not prove the volume mounted or contains audio.

The Bluetooth check requires Android 12 or newer and Nearby devices permission.
It checks for a Plaud name or compatible service advertisement for up to twenty
seconds while the screen is open. A Nordic manufacturer identifier alone does
not identify a Plaud Note. It does not retain device identifiers, pair, or read
recordings. No match does not prove the recorder is absent. Even after a match,
the device screen reports that direct connection is unavailable without
supported recorder authorization. It does not open GATT. A previous November
2026 Pixel 7 diagnostic build found a Plaud advertisement and enumerated GATT
services, but did not establish an authorized recorder session or read audio.
Plaud's [Embedded device support matrix](https://docs.plaud.ai/plaud-embedded/devices)
explicitly excludes the original Note and lists only Note Pro and NotePin S.
Its [SDK authorization](https://docs.plaud.ai/plaud-embedded/auth-api-overview)
uses partner-issued user tokens, not the account OAuth used by the localplaud
cloud poller. An existing Plaud Web login does not resolve recorder access.
The diagnostics do not provide direct, background, or Wi-Fi sync. For the
original Note, [Plaud's transfer guide](https://support.plaud.ai/hc/en-us/articles/50828234295577-Transfer-files)
says the official app transfers over Bluetooth once connected; Wi-Fi Fast
Transfer must be started inside the official app.

Connecting the Pixel to a development computer over USB only permits APK install
and inspection. A recorder charging nearby still has to advertise over Bluetooth;
the USB check is unrelated to discovery for that setup.

When the Note was connected directly to the development computer, Linux showed
a USB CDC communications/data peripheral but no new USB mass-storage interface
or block device. This is not a firmware version check or proof of a supported
flashing interface. No serial commands were sent and no firmware was changed.
Plaud's [firmware guide](https://support.plaud.ai/hc/en-us/articles/50962771415321-How-do-I-update-the-firmware-for-my-Plaud-Note)
documents forward updates through the mobile app, with recordings transferred
before flashing; it does not document a rollback or promise that one preserves
recordings and binding. Before considering any firmware change, obtain a
model-specific recovery procedure and data-retention confirmation from Plaud.

Direct sync remains experimental (issue #28). The original Note still needs a
verified, owner-authorized way to list and read recordings from its existing
binding. Do not reset or rebind the recorder or copy tokens out of the official
app. Once read access is verified, use an already-finished test recording to
validate transfer before asking for a new recording. Verify that the upload is
bound to the selected workspace, survives a connection interruption without
creating duplicates, and starts local processing without any Plaud AI artifact.
Keep the phone copy until the server confirms receipt, and never delete the
recorder copy. Then test automatic discovery after recording stops while the app
is in the background. A foreground Bluetooth connection is not evidence that
this trigger works. Wi-Fi acceleration requires its own hardware test.

#### Android automatic reconnection prerequisite

The Pixel 7 reports Android's Companion Device Manager feature and service, but
its current Android profile has no companion association for either localplaud
or the official Plaud app. This was a read-only query; no association was added
or migrated. A missing Android companion association says nothing about the
recorder's existing Plaud binding or ordinary Bluetooth bonds.

[Android's companion-device guide](https://developer.android.com/develop/connectivity/bluetooth/companion-device-pairing)
distinguishes an app's companion association from opening a Bluetooth link.
Presence events could trigger a connection attempt for an approved device, but
they neither establish the link nor authorize a Plaud recorder session. The
current Android diagnostic is scan-only. There is no authorized recorder
transport for the original Note to reconnect here, so no association prompt, background
connector, or OS-level presence simulation was run. Acceptance requires an
authenticated session with the physical Note followed by automatic recovery
after it leaves and returns to range, with no new prompt or manual connection.

`PlaudReconnectSimulationTest` exercises a test-only decision model with fake
presence events, authorization, transport, and a manually fired retry timer.
It covers returning to range, disconnects while still nearby, duplicate
notifications, missing or revoked authorization, and stale callbacks from an
abandoned attempt. That test's fakes are not part of the app. It checks the event
rules only, not Android background delivery,
recorder authorization, or reconnection to the physical Note.

The device screen uses `AndroidBleDiscovery` for a bounded foreground scan.
A matching advertisement is a presence hint, not proof of identity or
permission. Scan silence does not imply loss of an established link.
`BleConnectionManager` accepts sighting/loss events, but only invokes a
connector returned by its separate device authorization interface. The
connector must report an authorized recorder session, not just GATT success.
When a connector reports GATT service discovery, it must report discovery
success before session readiness. The same 15-second opening deadline covers
discovery and authorization; expiry closes only that attempt. A late GATT
callback cannot mark a session ready or affect its replacement.
The owner must serialize events and timers on one thread, report authorization
changes, and stop the manager with its lifecycle. The manager closes abandoned
connections, ignores stale events, and backs off after link failure.
Sync Plaud screens share one connection coordinator on Android's main thread.
After a fresh sighting and device authorization, only one controller owns the
candidate's connection, including any pending retry. A waiting screen gets a
turn only after the old controller closes its transport and cancels its retry.
It must check its own authorization again before opening a connection; no
transport or grant is carried between screens.
`BleConnectionManagerTest` runs this implementation with fake discovery events,
authorization, transport, and timers. Debug builds also expose a separate
**Bluetooth simulation** section: start the fake connection, simulate link
loss, then watch it reconnect without further input. Leaving the screen stops
the simulation and cancels pending retries.

On Android 12 or later, Debug also has a separate **BLE lab peripheral** panel.
Use a peripheral you control that advertises and exposes the GATT service
`85ffec0f-a47f-407b-b20c-b70260aea5d5`, with a stable Bluetooth address. Enter that
address, allow Nearby devices and explicitly authorize the lab session. The
client filters by both address and service, opens native GATT, and checks the
service without reading or writing characteristics. Disconnect and restart the
lab peripheral's advertisement: the session count should increase from one to
two without another tap. Another peripheral advertising the same name and UUID
at a different address is not a fallback during reconnect. The Debug app
remembers only the selected address.

Build the separately installable **Localplaud BLE Lab** APK without an init script:

```sh
gradle -p android -PlocalplaudOrigin=http://127.0.0.1:8000 assembleBleLab
```

Install `android/build/outputs/apk/bleLab/localplaud-android-bleLab.apk` and
open **Localplaud BLE Lab** from the Android launcher. Its application ID is
`org.localplaud.android.blelab`, separate from the regular app. The launcher opens
Sync Plaud directly; no workspace server is needed for the BLE lab. The launcher
and Debug BLE fixtures are not included in `assembleRelease`.

For a reusable Linux test peripheral, use a powered BlueZ adapter that supports
BLE advertising, plus the system Python D-Bus and PyGObject bindings:

```sh
python3 scripts/ble_lab_peripheral.py --adapter hci0 --peer-address AA:BB:CC:DD:EE:FF
```

Pass the Bluetooth address of your **test phone as seen by BlueZ**, not the
recorder. Android may use a rotating BLE address that differs from the address
in Settings. If needed, identify the phone by checking `bluetoothctl devices Connected`
while starting and stopping an isolated lab connection, then relaunch the fixture
with the observed address. Do not substitute another connected device if the
address changes. In the Android Debug lab, enter the Linux adapter's Bluetooth
address and authorize the first connection. After the fixture prints `CONNECTED=1`,
send `SIGUSR1` to its Python PID from another terminal. The fixture withdraws its
advertisement, disconnects only the specified test phone and resumes advertising
after four seconds by default. Without tapping again, the Android lab should
show two ready sessions. `Ctrl-C` removes the fixture. The script exports only
the custom test service, with no characteristics or recorder data.

Add `--decoy --offline-ms 8000` to keep advertising the same `LPLab` name
without the test service UUID for eight seconds after disconnecting. The lab
should stay at one ready session while the fixture reports `DECOY_ADVERTISING`,
then reach two only after the fixture restores `ADVERTISING` with the UUID.

Alternatively, use `--missing-service --offline-ms 20000` to keep the correct
UUID in the advertisement while the custom GATT service is unregistered. The
automatic reconnection should be rejected without adding a ready session or
retrying. After `GATT_RESTORED`, tap **Authorize and scan for lab peripheral**
to start a new session and reach one ready connection.

If Android throttles a recovery scan, the lab waits 30 seconds before the
next attempt. Duplicate callbacks do not add retries, and the round stops
automatically after three scan attempts without connecting.
The Debug panel then shows **Automatic reconnect stopped**. After the cooldown,
**Rescan for lab peripheral** starts a new bounded round only if the original
grant still applies. Nothing restarts automatically when the cooldown ends.
A delayed rescan request for an earlier round cannot start a later round, even
if both rounds use the same grant.
Rotating during the Debug cooldown or while Rescan is ready keeps the same
in-memory controller, round, grant, and deadline. It does not start a scan or
renew authorization. The detached screen can no longer request a rescan.
Repeated Bluetooth-on notifications without an intervening off event leave the
original throttle deadline and remaining scan budget unchanged. The retry checks
the original grant when the timer expires.
If the permission watcher detects that Nearby devices permission was revoked
during a recovery scan or backoff, the lab stops scanning and retrying, ends the
session, and shows the permission warning. Restoring system permission alone
does not resume scanning. Tap **Authorize and scan for lab peripheral** again
to grant a new lab session; it starts with a fresh ready-session count.
Turning Bluetooth off cancels the pending throttle retry. Turning it back on
starts one recovery scan only if the original grant still applies; the old
timer cannot start an extra scan.
A late adapter shutdown notification still invalidates the old GATT even if
the adapter has already switched back on. Recovery then checks the live
adapter state and the original grant; the notification does not grant access.
If the enabled adapter has scan permission but its scanner is not ready yet, the
Debug lab uses the same bounded retry budget without waiting for another on
notification. Each retry rechecks the original grant.
An asynchronous application-registration failure in the Debug lab also uses
this budget while Bluetooth and scan permission are available; receiving a
scanner object does not reset the budget.
If a scan result is delivered before scan startup returns, the lab still opens
at most one authorized link and releases the scan and its timeout.
Stopping the lab during the wait cancels the scheduled retry; neither late
scan callbacks nor the original deadline can restart the connection.
If the first connection or its service discovery never calls back, the 15-second
watchdog closes that GATT attempt. Recovery uses bounded rescanning and checks
the original grant again; late callbacks cannot mark the old link ready.
If a replacement link opened by a recovery scan times out instead, the lab backs
off and checks the original grant before starting a fresh scan. A discovery
success at the deadline cannot publish readiness, even if the watchdog has not
run. A late watchdog or result cannot duplicate or cancel the recovery scan.
A delayed Android scan failure, including throttling, is discarded at delivery
if its scan has stopped. It cannot stop an active recovery scan or put it back
into backoff.
Android scan results queued before a scan stops are discarded at delivery, even
if another scan is already active. The active scan also discards broadcasts with
timestamps before its own start; a timestamp equal to the start is eligible.
Batch callbacks apply both checks to each result, including after a result stops
the scan, and do not bypass the independent authorization check for reconnecting.
When a matching batch and a throttle callback are queued for the same scan,
whichever stops the scan first invalidates the other: the lab either reconnects
or backs off, never both for that scan.
Once a new link is ready, late failure callbacks from retired recovery scans
cannot change its state, close the link, or schedule another scan.
If Android returns `false` when asked to start service discovery or reports
`GATT_FAILURE` in the discovery callback, the lab closes that attempt immediately
and uses the same backoff, with authorization rechecked before reconnecting.
Successful discovery without the lab service rejects the connection without a retry.
If Android revokes connection permission after a scan result is queued, the lab
blocks the GATT open, shows the Nearby devices permission warning, and does not
retry automatically even if the independent grant is still valid.
The same no-retry permission warning applies if the precheck succeeds but
Android rejects `connectGatt()` with a `SecurityException`.
If Android denies Bluetooth access when starting discovery, the lab closes GATT
without retrying and shows a Nearby devices permission message. Check the
permission and explicitly authorize a new session before scanning again.
Once service discovery and authorization succeed, the next disconnect waits
one second before starting a fresh scan. Duplicate disconnects do not add retries.
The Debug transport regression test supplies Android GATT callbacks directly:
duplicate connected events start service discovery once, and discovery callbacks
from a closed GATT link are ignored during recovery.
If Android reports a disconnect before a queued connected event starts service
discovery, the lab skips discovery. If a discovery result is already queued,
the lab discards it instead. Both use the normal authorized recovery flow.
If a queued connected event is handled after the connection deadline, the lab
also skips discovery and recovers without waiting for the timeout task to run.
A queued discovery success handled after the deadline likewise cannot publish
readiness. The expired link closes and schedules one authorized recovery scan
through the existing backoff, even if the timeout task has not run yet.
When Android reports a service change, the lab marks the link not ready and
rechecks authorization before rediscovering on the same link. Notifications
queued before rediscovery starts are coalesced. A new notification after it
starts invalidates the result and triggers the existing authorized recovery scan
flow instead of restoring readiness or starting another discovery on that link.
The notification sequence belongs to its GATT link: a delayed notification
from a closed link cannot invalidate a replacement link's rediscovery.
A successful recheck does not count as a new connection.
If the recheck reaches its 15-second deadline, the lab closes the link and
uses the same authorized recovery scan and backoff whether the watchdog or a
late successful callback is handled first.
If services change during the initial discovery, that result is discarded even
when a successful callback was queued first but has not yet been applied.
After its callback, the lab rechecks authorization and runs one follow-up
discovery on the same GATT link, within the original connection deadline. If
that deadline has already passed, the lab skips the follow-up and uses the
same recovery backoff, even if the timeout task has not run yet.
If services change again during that follow-up, the lab closes the link without
publishing readiness or starting a third discovery on it, even when a successful
result was queued first but has not yet been applied. It uses the existing
backoff and bounded recovery scans, subject to fresh authorization checks.
After closing the screen or restarting the process, the saved address is
displayed but nothing scans or connects until the user authorizes again. This
starts a fresh scan and GATT session; no grant or live connection is restored.
While the lab panel stays open, switching Bluetooth off closes the old GATT
session and cancels pending scans and retries. Switching it back on starts a
fresh scan only if the original lab grant is still valid; a replacement grant
cannot resume the cancelled retry. Results and timeouts from a scan interrupted
by Bluetooth turning off cannot open a link or disturb the fresh scan. If a
recovery scan has already opened a GATT link, turning Bluetooth off also closes
that pending link. Across repeated off/on cycles, late callbacks from earlier
links cannot mark them ready or affect the newest link. An old disconnect timer
cannot start a second scan, even if its callback arrives late.
A duplicate Bluetooth-on notification during recovery discovery does not restart
the scan or replace the GATT link. Readiness still requires the original grant.
During the backoff after a recovery link times out, duplicate on notifications
also leave the retry deadline and scan budget unchanged.
Stopping the lab while Bluetooth is off clears the pending recovery; turning
Bluetooth back on will not scan until the user chooses **Authorize and scan** again.
When the Debug app observes that Android's Nearby devices permission was revoked,
it ends the lab session and clears the saved reconnect intent. Restoring system
permission does not restart that session, even in a fresh process.
To check cold-start recovery, keep the lab peripheral advertising, revoke
Nearby devices in Android Settings, then reopen the Debug app. Restore the
system permission without authorizing the lab session. If the app opens while
the permission is still missing, it clears the saved reconnect intent before
creating any BLE session. There should be no new scan or connection until you
enter the address and press **Authorize and scan**. The peripheral should then
see exactly one new GATT connection.
If loading the saved lab device fails, **Retry reading saved device** refreshes
the saved address without starting a scan or granting access.
**Stop lab test and forget device** revokes the in-memory grant, cancels scans
and links, and clears the saved address. Delayed GATT callbacks from the ended
session cannot change or close a newly authorized session. Each connection attempt
is bound to the grant it opened under; a replacement grant cannot make an old
GATT callback publish readiness. Recovery scans and their backoff timers are
bound to the original grant too: neither a pending timer nor a queued sighting
can start work under a replacement grant. When notified of revocation, the lab
leaves the retry state immediately; a cancelled timer cannot affect a later
authorized retry. The address and test UUID are filters,
not proof of recorder identity. The panel and its `BLUETOOTH_CONNECT` permission
are absent from Release.

The screen observes Android's
Bluetooth adapter while visible. Switching it off stops scanning, closes the
transport, cancels pending retries and invalidates previous sightings. The
active owner keeps its claim while the screen remains active; waiting screens
discard theirs. On reopening Bluetooth, only that owner automatically rescans
for a fresh sighting, after rechecking recorder authorization. Other screens
need a new sighting to request a connection. Each scan lasts up to
20 seconds. A timeout or exhausted Android scanner resources schedules another
scan after 2 seconds, then 4 seconds. Android scan throttling instead waits
30 seconds before the next attempt. All three outcomes share a cap of three
scans. After exhaustion, **Rescan** becomes available after a 30-second cooldown.
Nothing restarts automatically at the deadline: tapping Rescan starts a new
three-scan budget for the same candidate. Toggling Bluetooth does not skip this
cooldown. Before each managed scan starts, the app checks the saved budget revision
and commits the debit together under one lock shared by all controllers in the
app process. A stale revision or storage failure prevents the scan from starting.
On a revision conflict, the screen reloads the saved attempts and cooldown in
place. At the refreshed deadline it checks the journal again without starting a
scan. The user must tap **Rescan** to proceed. If the journal was cleared, the
screen returns to manual discovery; a different candidate requires an explicit
status refresh before the screen adopts it.
Rotation and same-boot process recovery retain the candidate, consumed attempts and
deadline, but do not restart a scan or restore a connection or stored authorization.
An interrupted scan counts against the existing budget and can leave up to a
50-second wait from its start. After the wait, the user can resume the remaining
attempts or begin a new round if the budget was exhausted. An unknown or different
boot does not restore the saved round.
Missing permissions, revoked recorder authorization,
and unknown errors stop retries. Turning Bluetooth off or leaving the screen
cancels pending rescans. Delayed results, failures and timeouts from a cancelled
scan cannot change the screen or interrupt a newer scan. Only a matching result
from the active scan can trigger a connection attempt, and
only after recorder authorization is checked again. Debug builds inject a
simulated scan result; Release builds include neither the simulator controls
nor the fake transport. There is no Android background
service or real recorder transport; physical reconnection remains unverified.

#### Official account and multi-phone support for the original Note

Plaud's [switching phones guide](https://support.plaud.ai/hc/en-us/articles/51284640281881)
(updated 2026-11-02 UTC) says the device is bound to the account, not the phone.
A replacement phone can use the official app with the same account and sign-in
method without unbinding. The [multi-device account FAQ](https://support.plaud.ai/hc/en-us/articles/62147790266905)
(2026-11-18 UTC) separately confirms that the same account can sign in on two
phones but each recorder connects to only one at a time. The
[original Note sharing FAQ](https://support.plaud.ai/hc/en-us/articles/52137941760409)
(2026-11-20 UTC) says a connected Note is not discoverable or connectable from a
second phone. These articles document the official app, not independent clients.
The [Mobile App v4.0.x+ binding guide](https://support.plaud.ai/hc/en-us/articles/63439108932121)
(2026-11-20 UTC) likewise describes account binding through the official app.

The [Embedded binding lookup and recovery documentation](https://docs.plaud.ai/api-reference/device-binding-api/get-device-binding)
describes a registry and binding history for recovery, not a way to authorize a
second active client while preserving the existing binding. The procedure is
in the Embedded SDK's scope, which excludes the original Note. No reviewed
public Plaud document confirms that the original Note can grant device-level
access to an independent Android app under an existing consumer account
binding. This is a documentation gap, not proof that no such mechanism exists.

#### Original Note firmware and connection guidance

Plaud's [original Note firmware guide](https://support.plaud.ai/hc/en-us/articles/50962771415321)
(updated 2026-11-08 UTC) describes checking the installed version and applying
an available update in the official mobile app. It is not a version-by-version
firmware changelog. The [USB support article](https://support.plaud.ai/hc/en-us/articles/12334579722767)
(2026-11-16 UTC) documents a specific change at V2.1: USB and RAW-file access
were disabled. Neither source documents third-party client authorization.

Cross-checking a separate path, the [original Note connection troubleshooting
guide](https://support.plaud.ai/hc/en-us/articles/8127930391183) (2026-11-18 UTC)
covers first connection and reconnection to the official app; it asks whether
the recorder appears in a BLE diagnostic app, not whether that app can open an
authorized session. The [product security policy](https://support.plaud.ai/hc/en-us/articles/59556140012313)
identifies the original Note as NB-100 and distinguishes security maintenance
from feature updates. No reviewed public release note documents a firmware
version with opt-in independent Android authorization that preserves the existing
binding and on-device audio. This does not establish the firmware installed on
the test recorder or rule out undocumented versions. Only public documents
were accessed; no firmware update, pairing, or connection was attempted.

#### Plaud Embedded partner eligibility and existing hardware

Plaud's public [partner intake](https://dev.plaud.ai/contact/) asks for the
company, work email, expected users, and use case. It does not state a model
exception for existing original Note (NB-100) owners. The [pricing FAQ on using
an already-owned Plaud device](https://dev.plaud.ai/pricing/) specifically names
the Note Pro and NotePin S as SDK-compatible. The [Embedded onboarding guide](https://docs.plaud.ai/plaud-embedded/how-plaud-embedded-works)
describes binding compatible hardware to the partner's app and says a device
is linked to one application at a time. This is an application-level constraint
in Embedded, distinct from the official app's same-account phone switching.
The public FAQ and onboarding docs do not give a supported way to add a second
independent app to an existing consumer binding, or guarantee that the original
binding and on-device audio survive an Embedded enrollment. The public customer
case study index did not establish an NB-100 exception either. Reviewed on
2026-11-23 (Taipei time); no application or request was submitted.

#### Community interoperability references

The [Raspberry Pi report](https://www.reddit.com/r/PLAUDAI/comments/1y0zv71/comment/19sx2cydm5wro/)
describes a **Note Pro**, not the original Note. Its references,
[OpenPlod](https://github.com/RemiPelloux/OpenPlod) and
[plaude-archiver](https://github.com/roopen219/plaude-archiver), are useful
architecture examples: keep BLE transport, authorized recorder sessions, and
durable file ingest separate. The reported transfer still requires an enrollment
identity; it does not establish that an existing original Note binding can
authorize localplaud. The parent post uses the official app for device-to-cloud
transfer and only replaces what happens after upload.

[OpenPlaudit](https://github.com/mar-maradnus/openplaudit) describes direct BLE
transfer for the original Note, but assumes a binding credential issued at
initial pairing. [OpenPin](https://github.com/StayAwhileAndCode/openpin) also
requires a device key and marks original Note support and Bluetooth-only
downloads experimental. Neither reference establishes a supported way for an
owner to grant another app read access without extracting credentials or changing
the current binding. Do not port a Note Pro handshake or treat Android pairing
as a substitute for original Note file authorization.

The existing authenticated `POST /api/imports/local/audio` accepts an optional
`upload_id` (a UUID generated once and reused across retries) with the current
`capture_scope`. Retrying the same bytes returns the original recording receipt;
reusing an ID for different bytes or with a stale workspace scope returns 409.
This is an ingest primitive, not authorization to download from the recorder
or a way for the APK to access the browser's session. On an account-mode server,
the browser can explicitly approve the phone's challenge at
`/account/android-upload`. Within five minutes, the phone can claim it once by
sending `Authorization: Upload-Proof <verifier>` to `POST
/api/imports/local/android-grant`. The verifier is a random 32-byte value encoded
as unpadded base64url; the browser only sees its unpadded base64url SHA-256 digest
(computed over the encoded verifier). The credential expires within one hour or
when the issuing session expires or is revoked. It is only accepted
on the POST audio route, for the issuing workspace and with a stable upload ID;
it does not grant library access or permission to control Plaud.

The Android recording queue can persist a complete transfer, reuse its upload
ID after reopening, and retain the audio until a valid receipt is recorded.
Directory entries are flushed before acknowledging durable staging or receipts.
Malformed entries are isolated without deleting their audio or blocking valid
entries. Permanent HTTP rejections are retained for review; transient failures
remain pending, and expired authorization stops the batch. The Debug upload
status reports uploaded, retryable, retained, and unreadable entry counts.
Android-grant imports record their upload channel and issuing session, and honor
an explicitly supplied recording timestamp after validating it is not in the
future. A missing recording timestamp still uses the ingest time.
Its upload client sends audio to the configured workspace origin without
following redirects. It clears the phone copy only after an HTTP 201 response
with matching `X-Localplaud-Upload-Id` and a valid `X-Localplaud-Recording-Id`.
Debug builds can open the browser approval page, display the matching comparison
code, and claim a grant to upload entries already staged in app-private storage.
This is a manual diagnostic, not a recorder reader or a background scheduler.
Future recorder transports must key entries by an immutable recorder/recording
identity within the approved origin and workspace. In a Pixel 7 test with an
isolated server, the phone and browser showed the same code and browser approval
succeeded. The device's per-app network policy blocked the native claim request;
no audio was sent, and the network policy was left unchanged.

The server integration test uses synthetic WAV audio and stub speech/AI
providers. It verifies that an authorized upload lands in the approved private
workspace, the daemon processes it without Plaud, the original bytes remain
unchanged, and a retry after processing returns the same receipt without
queueing another recording. This does not verify physical Note file access,
production providers, or the stop-recording trigger.

The HTTPS debug APK installed on Pixel 7 showed **Sync Plaud** in the launcher
menu. A follow-up confirmed both apps were installed but could not exercise the
cross-app launch because another device session held the phone. The
recorder-to-workspace path remains unverified. The production association
endpoint is now available for the signed release; that does not associate a
debug APK signed with a different key.

In an isolated Pixel 7 test, browser approval succeeded but Android reported
restricted networking and denied the native app's connection. On Android 10
and later the debug screen reports when Android blocks this app's default
network. This warning is independent of the browser approval result. The test
did not change phone network policy or send any audio.

After enabling network access for localplaud only in the Pixel's app settings,
a second isolated test approved a synthetic workspace in the phone browser.
Returning with Android Back preserved the pending request in the app; the
native claim returned HTTP 200 and reported an empty queue. This verifies the
browser-to-APK handoff over USB loopback, not a production upload or recorder
authorization. No audio was sent.

### Import a copy already on the phone

If an audio copy is already available through an authorized export or backup,
open localplaud on the phone, select **Import audio → From this device**, and
choose that copy in Android Files. This does not contact the Note or Plaud.
It cannot recover a recording that exists only on the recorder.

On November 23, 2026, this path was tested on Pixel 7 in Brave against an
isolated localplaud workspace over USB loopback. A two-second synthetic WAV
selected from Downloads appeared as **Local upload**, with status `downloaded`;
the stored WAV's SHA-256 matched the source. The worker was not run in this
device test. A separate server integration test verified local processing and
retry deduplication with stub speech/AI providers. No recorder connection,
production deployment, or official Plaud sync was necessary.

The development host's test environment had no Whisper, WhisperX, or pyannote
runtime or cached speech model available. No models were fetched and no account
was used. Separate offline regression cases verified that missing WhisperX or
pyannote marks the relevant stage degraded while keeping the transcript usable.
A retry with a test provider reuses the existing transcript, preserves edits,
and records the recovered stage. This verifies failure and recovery behavior,
not inference quality, real two-speaker diarization, or Note access.

### Verify your daily sync workflow

Use a new, short recording with non-sensitive content. Confirm the recorder
has uploaded it in the official Plaud app, without selecting **Generate**.
On a newly connected workspace, complete the first catalog check **before**
uploading the test recording. The first check registers an existing catalog
without automatically downloading its history.
Run **Check Plaud now**, or wait for the next server poll
(`[poller].interval_seconds`, 300 seconds by default), then find the recording in
**All files**. Processing can take longer
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

GitHub Releases include two distinct APKs: `localplaud-android-VERSION-CODE.apk`
is the signed workspace app, and `localplaud-ble-lab-VERSION-debug.apk` is an
optional Debug app with package suffix `.blelab` and a direct BLE Lab launcher.
Install the Lab alongside the workspace app only for custom test peripherals.
Its Debug signing key is not a durable update identity; a later Lab APK may need
an uninstall/reinstall. Never use the Lab as proof of original Note access.

To reproduce the release variant checks locally, run:

```sh
gradle -p android -PlocalplaudOrigin=https://plaud.example.com testDebugUnitTest testBleLabUnitTest testReleaseUnitTest lintDebug lintBleLab lintRelease assembleDebug assembleBleLab assembleRelease
python3 scripts/check_android_variants.py
```

The packaged-manifest check verifies launcher/package separation and confirms
the release app has no Bluetooth connection permission or Debug BLE transport.

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

From the checkout, verify the public health, login, and certificate association
without signing in (use the APK certificate digest you read above):

```bash
./scripts/deploy/smoke-test.sh https://plaud.example.com "$APK_CERT_SHA256"
```

This takes only a public fingerprint, not the signing key. A pass confirms the
origin serves the required statement; still launch the signed APK on the device
and confirm standalone mode and account sign-in.

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
