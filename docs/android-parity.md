# Android workflow parity backlog

Status: implementation and verification notes, 2026-11-20. Compares localplaud's phone layout (served
through the Android TWA described in [android.md](android.md)) with the Plaud
App patterns recorded in [plaud-mobile-app.md](plaud-mobile-app.md), under
the rules in [product-workflow.md](product-workflow.md).

Scope rules carried over from those documents:

- Parity means matching the workflow and interaction model. localplaud keeps its
  own branding and assets ([brand.md](brand.md)).
- No feature may depend on Plaud AI output (Generate, transcripts, summaries,
  Ask).
- Device pairing, Bluetooth/Wi-Fi transfer, and replacing raw-audio sync are out
  of scope. The official Plaud App still uploads raw audio.

Evidence labels:

- **Observed (source)**: read in this repository's templates, CSS, JS, or API
  code during this audit. The rendering was **not** checked in a browser or on a
  device.
- **Unverified**: an inference about runtime behaviour. It needs a browser or
  device check at phone width.
- References to Plaud behaviour use the evidence tags in
  `plaud-mobile-app.md` ([H]/[D]/[S]/[B]). On the physical Pixel 7, Plaud
  Android 4.4.0 (Play build 1337) exposed Home, Library, Ask Plaud, Tools, and
  Settings before sign-in.
  Signed-in onboarding showed Chat and Work modes. The Work tutorial exposed
  a home drawer, Add device, New, Calendar view, and Ask Plaud. This is a
  partial audit; the older screenshots below are not a verified map of the
  signed-in build. No purchase or generation was triggered.

## Comparable workflows (observed in source)

| Plaud pattern | localplaud implementation |
|---|---|
| Three-slot bottom bar (Files · + · Explore) | `src/localplaud/api/templates/_tabbar.html`; hidden ≥768px and while the keyboard is open (`static/css/app.css`) |
| Root top bar: scope ▾, search/Ask, avatar → Settings | `templates/_m_root_topbar.html` (left slot is the local processing status, which opens the drawer) |
| Filter & sort sheet: sort, All/Unfiled/Trash, folders with +, Comes from | `templates/index.html` (`#library-sheet`), plus a localplaud Status group |
| Swipe-left actions, long-press, select mode | `static/js/app.js` (phone list gestures), `templates/index.html` (`data-select-mode`) |
| Search with "Ask the library" entry and Files/Content results | `templates/search.html` |
| Detail header Sources \| Notes switch and note picker | `templates/detail.html` (`.ws-m-switch`, `.ws-note-picker`), `static/js/workspace.js` (phone header switch) |
| Outline with timestamps on Sources | `templates/detail.html` (`.ws-outline`) |
| "Name this speaker" sheet with paragraph/all scope | `templates/detail.html` (`#ws-speaker-sheet`), `static/js/workspace.js` |
| "Name speakers" with sample clips | `templates/detail.html` (`name-speakers` dialog), `static/js/workspace.js` |
| Find & replace from the header | `templates/detail.html` (`.ws-find`) |
| Pinned Ask input (opt-in with Show Ask) | `templates/detail.html` (`.ws-ask-pin`) |
| Auto / Custom generation choice | `templates/detail.html`, `static/js/workspace.js` |
| Explore hero band (local pipeline status, not a plan) | `templates/explore.html` |
| Settings profile stats and 52-week heat map | `templates/settings.html` |
| One grouped Share/Export sheet with in-sheet options | `static/recording-share.{css,js}`, `templates/_recording_share.html` |
| Lock-screen controls | `MediaSession` in `static/js/app.js` and `static/js/workspace.js` |
| Calendar view from Home | `/calendar`: workspace-local timezone, day counts, and date-filtered links to Files; verified with synthetic data on Pixel 7 |

## Ranked gaps

The ranking weighs how often a phone user hits the gap against the cost of
working around it. "Backend" says whether the API already supports the slice.

| # | Gap | Plaud reference | localplaud source (observed) | Backend | Evidence |
|---|---|---|---|---|---|
| 1 | **Phone multi-select and batch Generate notes implemented.** Generation previews eligibility and requires confirmation; Merge and cloud Delete are deliberate divergences. | §2 Multi-select: "Selected (N)" title, Done pill, floating action card (Generate, Move, Rename, Merge, Delete) [H] | `templates/index.html` (`#bulkbar`), `static/css/app.css` | Existing organize, resume, local processing deletion, and bulk export; `/api/files/generate-notes` queues derived-only work | Selection, Rename eligibility, and Done verified on Pixel 7; batch generation preview and queue confirmation verified only with synthetic Chromium at 390 and 1280px |
| 2 | **Recently used names implemented.** Tapping a name fills the input; saving the rename remains a separate action. | §3 Speakers: *Recently used names* list with an empty state [H] | `static/js/workspace.js`, `speaker_names.py` | Read-only, workspace-scoped `GET /api/speaker-names/recent` | Account-isolation tests and synthetic Chromium at 360 and 1280px; chips rendered on Pixel 7 |
| 3 | **Generate notes added to the swipe tray and row menu.** The action is limited to eligible recordings with a local transcript and opens the confirmation form without queueing work. The phone row menu is a visible 44px touch target. | §2 Swipe left: Generate, Move to, Rename, Merge, Delete [D] | `static/js/app.js`, `templates/_library.html`, `templates/detail.html` | Uses existing local generation form. Delete and Merge are deliberate divergences (below) | Both entry points opened the form on Pixel 7; no generation submitted |
| 4 | **Mind map preview implemented below Notes.** The card opens the existing canvas without reloading playback or triggering processing. | §3 Notes: bordered "Mind map" preview card with expand ↗ [H] | `templates/detail.html` (`data-mindmap-preview`), `static/js/workspace.js` | Existing mind map artifact | Synthetic Chromium at 360, 412, and 1280px; mobile touch, focus, and Back checked |
| 5 | **Sources has expandable playback controls.** Waveform and seek, ±15s, and playback speed reuse the docked player, including its audio and position. Opening the full transcript remains a separate action. | §3 Sources: expanded inline player with waveform, ±15s, speed, collapse [H] | `templates/detail.html`, `static/js/workspace.js`, `static/css/recording.css` | Uses the existing audio and waveform endpoints | Synthetic Chromium at 360, 412, and 1280px; touch seek, waveform fallback, tab switches and library/back continuity verified. Physical device unverified |
| 6 | **Android has a Sync Plaud launcher shortcut** in addition to the All files, Search, and Explore shortcuts shared with the browser install. Sync Plaud opens the official app, which still owns recorder transfer and raw-audio upload. | n/a (platform) | `static/manifest.webmanifest`, `android/src/main/res/xml/shortcuts.xml`, `PlaudSyncActivity` | Server processing remains automatic via the existing poller, when enabled | Sync Plaud is visible on Pixel 7; cross-app launch and recorder sync not yet verified |
| 7 | **Foreground recording and opt-in stopped-draft recovery are implemented.** Capture attachments, background recording, and recovery before Stop are not. | §9 Capture: Start recording with flags, photos, and notes [S][D] | `templates/base.html` (`add-sheet`), `static/js/app.js` (`MediaRecorder`), `static/js/capture-drafts.js` | Unsaved drafts stay in the browser; Save reuses `/api/imports/local/audio` | Physical Pixel 7 recorded, paused, resumed, stopped, and discarded without saving; draft recovery and account separation verified in synthetic Chromium, not on Pixel |
| 8 | **No app lock.** | §8 Preferences → Security → App lock [H] | `templates/settings.html` (absent) | No | Source observed. Likely better left to the OS or browser in a TWA |

## Deliberate divergences (do not "fix")

- **Delete/Merge in list actions**: Trash is a read-only mirror of Plaud state,
  and normal operation is read-only against Plaud. A local hide or archive
  feature would need its own design. Bulk "Delete local processing" removes only
  derived artifacts.
- **Ask entry points are off by default** (`ask_enabled`). Plaud always shows
  them.
- **Speaker colour dots, tags, and a Status filter group**: these are localplaud
  additions that the product spec requires.
- **Template community, Publish, Photo to template, and Share → Invite**:
  localplaud has no network community or multi-user invite. Discover shows
  local/bundled packs.
- **Plaud wordmark, artwork, and the cyan-green AI gradient** are never used. The
  localplaud blue→violet accent applies.

## Implemented slice: phone selection action card (gap 1)

The phone card reuses existing endpoints, without schema changes, Plaud calls,
or AI dependencies. Synthetic Chromium checks covered 360, 390, 412, 800, and
1280px layouts, including real Move and Tag requests and the Resume error state.
On Pixel 7, selecting one and then two recordings updated action eligibility
and Done exited selection mode. The original implementation scope is recorded below.

1. At <768px in select mode, replace the title with **Selected (N)** and add a
   black **Done** pill that exits select mode. Hide the tab bar while select mode
   is on.
2. Replace the two `<select>`s and Apply with a floating **action card**: two rows
   of icon-over-label buttons, each at least 44px.
   - **Move**: opens the existing move sheet in multi-file mode and posts to
     `/api/files/organize` with `folder_id`.
   - **Tag**: a sheet with add/remove tag rows. It posts `add_tag_ids` /
     `remove_tag_ids`.
   - **Rename**: enabled only when exactly one recording is selected. It reuses
     `lp.rowAction(…, 'rename')`.
   - **Export**: opens the existing bulk export dialog.
   - **Resume**: calls `/api/files/bulk` with `action: resume`. Disabled
     buttons stay visible with `aria-disabled` and a reason in the label.
   - **Delete local processing** (red): reuses the existing confirm.
3. Keep the desktop `.batch-bar` unchanged at ≥768px.
4. Add i18n strings for zh-TW and en. Respect `prefers-reduced-motion`. Close
   the card with Esc and Done, and return focus to the "…" trigger.
5. Tests:
   - a render test asserting the phone card markup and that Rename is disabled
     with more than one selection;
   - the existing organize, bulk, and export API tests stay green;
   - a browser check at 360, 390, and 412px covering empty, one, many, error
     (409 processing), and long folder names;
   - the TWA on a physical device once the Android shell is ready.

Batch Generate notes uses a separate endpoint and is now available. Merge and
cloud Trash changes remain deliberately out of scope.

## Remaining device acceptance

**Cloud handoff:** Home now offers **Check Plaud now** when the workspace has
a Plaud connection. The phone Add sheet has the same cloud check. Both show the
number queued for download separately from the number found, and link directly
to the new recording or library without leaving the dialog over the destination.
The check does not request Plaud Intelligence data. A regression test runs the
catalog check, raw download, worker, playback response, transcript export and
notes export against a raw-audio-only fake Plaud client and fake AI providers.
Chromium exercised the check, error and empty states, recording link, audio and
caption export with synthetic data at 360, 412 and 1280 CSS px.

The USB-connected Pixel 7 ran the debug APK against a synthetic workspace in a
Brave custom tab. This is not a verified standalone TWA. The production HTTPS
association endpoint returned `401` during the read-only smoke check; public
health and the sign-in page responded successfully. The debug APK build, lint
and unit tests passed, but the latest Pixel 7 run did not verify the new screen:
the browser resumed an older custom tab and the debug activity had no matching
intent for the attempted test URL. The previously installed APK was restored.

Before closing production acceptance:

1. Build and sign the APK for the final HTTPS workspace origin, publish the
   matching certificate digest, and run the public Android smoke check.
2. On Pixel 7, confirm standalone launch, sign-in, the **Sync Plaud** handoff,
   and library actions with the signed APK.
3. Upload one short test recording from a real recorder through the official app
   without Generate. Confirm discovery, local processing, audible playback,
   speaker/transcript editing, notes, and export in the production workspace.

Ask Plaud and AutoFlow are not part of this Android acceptance. Direct recorder
transfer remains tracked separately in issue #28.
