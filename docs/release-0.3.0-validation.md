# 0.3.0 validation and remaining acceptance

This release packages the existing workspace app, foreground recorder diagnostics,
and a separate Debug BLE Lab. It does not implement authorized original Plaud Note
recording transfer. No recorder binding, firmware, or original audio was changed.

## Automated and browser evidence

- Python regression coverage includes upload-only authorization, workspace
  isolation, stable upload receipts, retry deduplication, and a synthetic audio
  upload through the local pipeline using test providers.
- Missing WhisperX and pyannote dependencies produce degraded stages; restoring
  the test dependency path resumes without replacing successful ASR or edits.
  These are recovery tests, not real speech-model quality measurements.
- Android tests exercise authorization, scan/connection ownership, late callbacks,
  Bluetooth cycles, bounded retry budgets, permission loss, process recreation,
  and persistent reconnect intent using controlled doubles.
- Real Chromium at desktop 1440×1000 and mobile 412×915 verified generated-title
  search, empty results, and actual synthetic WAV playback in an audio-and-notes
  share without transcript access.
  Both widths had no horizontal overflow. The fixtures contained synthetic data.
- All three packaged variants are checked for their launcher, package name, and
  debuggability. The release APK excludes the Debug BLE transport and has no
  Bluetooth connection permission; BLE Lab uses a separate package and launcher.

Release preparation passed 205 Debug unit tests, 84 BLE Lab unit tests, and 84
Release unit tests with zero failures or skips, plus lint and APK assembly for
all three variants. The local Python suite passed 1,971 tests with 33 explicit
skips; the generated-title branch additionally passed its 71 related tests.
GitHub CI reruns the complete combined suite on Python 3.11 and 3.12 before merge.

An independent Codex visual check was attempted but its sandbox could not open
local TCP sockets or initialize computer use. Its attempt supplies no additional
UI acceptance evidence. The direct Chromium checks above completed successfully.

## Physical test peripheral evidence

On November 23, Pixel 7 and a Linux BlueZ peripheral exposing only the custom lab
UUID completed three successive disconnect/re-advertise cycles under one explicit
test authorization. Ready sessions advanced 1 → 2 → 3 → 4 without another tap.
Device logs recorded four GATT opens, four successful service discoveries, and
three old-link closes before their replacements opened.

A same-name advertisement missing the test UUID did not reconnect. Restoring the
UUID resumed once. Advertising the UUID while withholding its GATT service was
rejected without becoming Ready or continuing retries; restoring the service
required an explicit new start.

These observations establish the custom peripheral lab behavior only. They are
not proof of original Note compatibility, device authorization, file access,
recording-stop detection, Wi-Fi transfer, or background Android execution.

## Open acceptance

| Issue | Remaining evidence or prerequisite |
| --- | --- |
| [#28](https://github.com/skyhong2002/localplaud/issues/28) | Supported original Note authorization and recording access preserving its binding and originals. Public documentation has not established this path. |
| [#31](https://github.com/skyhong2002/localplaud/issues/31) | Pixel native queued audio upload to the intended authenticated HTTPS workspace, interrupted retry, and byte equality. Browser import, native loopback grant claiming, and server doubles are separate evidence. |
| [#32](https://github.com/skyhong2002/localplaud/issues/32) | Actual Android permission revocation during lab recovery and explicit restoration. Controlled permission-source tests pass; physical OS behavior remains unverified. |
| [#34](https://github.com/skyhong2002/localplaud/issues/34) | Original Note stop-recording detection, automatic transfer, background lifecycle, and return-to-range recovery, dependent on #28. |
| [#35](https://github.com/skyhong2002/localplaud/issues/35) | Authenticated production workflow and real configured local speech-model acceptance with available models. |

Do not close these issues on the strength of simulation, scanning, GATT
enumeration, or the separate official-app/cloud workflow.
