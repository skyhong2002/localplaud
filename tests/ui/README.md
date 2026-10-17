# Note recovery and speech rebuild browser regression

Serve this checkout with a static-only server; do not start a configured app:

```sh
python3 -m http.server 8897 --bind 127.0.0.1
```

Open `http://localhost:8897/tests/ui/note-recovery.html`. Run **Run regressions**
at desktop and phone widths, or evaluate `await window.runRegressions()` in the
browser. The returned report contains viewport, pass count, and per-case results.
`window.resetRecoveryFixture()` restores an editable synthetic note for manual
keyboard checks. **Show conflict recovery** and **Show rebuild failure** expose
the corresponding UI states for visual review.

The fixture loads the production note editor, workspace JavaScript, CSS, and
vendored HTMX runtime.
Its note/form selectors match the workspace template. It intercepts every
JavaScript API request with a closed allowlist, generates a silent WAV in memory,
and uses no app state, accounts, private media, worker, model, or external service.
Download checks intercept the click and inspect the generated Blob; they do not
write files. Clipboard tests replace the clipboard API and deliberately deny the
fallback. The fixture normally declines discard confirmations; explicit-discard
scenarios accept them. A closed fake XMLHttpRequest transport drives actual HTMX
requests and DOM swaps with manually ordered navigation responses. It cannot
send traffic to an app or even to the static server.

The scenarios cover:

- 409 and ExistingCopy responses with additional typing during the request,
  exact serialized Markdown recovery, retained DOM, no subsequent writes, and
  native/HTMX navigation protection;
- pending PUT/editable-copy navigation, navigation requested before editing,
  conflict-before-navigation and navigation-before-conflict response orders,
  request-scoped explicit discard, unrelated fragment updates, and successful
  saves followed by ordinary navigation without a discard prompt;
- earlier saved versions restored to the generated text, which must still be
  protected from an edit on the generated panel;
- comparison loading, unavailable/missing/empty/latest saved content, without
  adopting the remote version or replacing the local draft;
- clipboard success/denial, exact download content, accessible labels, long
  Unicode content, touch target sizing, and horizontal overflow;
- successful edits, optimistic base-version advancement, new generated copies,
  transient save failure and retry;
- speech rebuild 409/502/503/network failures, successful retry, settings-only
  saves, and failure before settings have been saved.

Checked on 2026-10-18 in the T3 collaborative Chromium browser at 1280×800 and
390×844: **23/23 scenarios passed at each size**. The recovery comparison was
also visually inspected at both widths. JavaScript syntax checks passed.
The existing isolated Python suites for manual notes UI, user note history,
speech overrides, web UI, and modal visibility, plus the recovery/rebuild catalog
coverage, passed: **84 tests**, with outbound sockets blocked. Only existing
dependency deprecation warnings were reported.

The expanded fixture was also run from a temporary static-only copy with
`note-editor.js` restored to `3272bb717a6f3a0c76213fdc4e0315767d9d467b`.
That negative control passed 14/23 cases: nine added guard assertions failed,
including actual removal of recovery by late HTMX swaps for both PUT 409 and
ExistingCopy. This check leaves the worktree's production script untouched.

Run the isolated Python checks with this worktree first on the import path and
the sibling runtime suite's outbound guard (use an existing test environment):

```sh
task_guard_dir=$(mktemp -d)
cp ../lp-runtime/tests/runtime_offline.py "$task_guard_dir/runtime_offline.py"
PYTHONPATH="src:$task_guard_dir" /path/to/test-venv/bin/python -m pytest \
  -p runtime_offline tests/ui/test_note_recovery_i18n.py \
  tests/test_manual_notes_ui.py tests/test_user_note_history.py \
  tests/test_speech_overrides.py tests/test_web_ui.py tests/test_modal_visibility.py -q
```

## Recovery boundary and integration hooks

Conflict recovery preserves the live rendered draft and an exact snapshot of its
current Markdown serialization. It stops autosave, source editing, and replacement
of that blocked draft. The comparison is read-only; the saved note opens in a new
tab, and recovery requires an explicit copy/download and manual merge. There is
no force-save path, revision adoption, or automatic overwrite of saved edits.
Copying/downloading does not clear the unsaved-navigation protection.

Drafts remain in page memory until explicitly downloaded/copied. They are not
automatically persisted into browser storage, where a different account could
otherwise inherit note content. Refresh/close warns through `beforeunload`;
HTMX navigation replacing `#app-view` (or an ancestor) requests explicit discard
confirmation while a draft is dirty, saving, or blocked by a conflict. The guard
runs before the request and again before the response swaps the page, because
editing or conflict recovery can begin after navigation was requested. Approval
belongs to that request only, so an accepted navigation can finish after a late
conflict without a second prompt; another navigation still asks. A successfully
saved draft needs no discard confirmation at the swap boundary.
Browser/process crashes and history-cache restoration cannot guarantee recovery;
download the draft for a durable copy. This is a first-pass frontend boundary,
not account-scoped server draft storage.

No backend files were changed. Useful follow-up contracts:

- Add `GET /api/notes/{id}` returning the current authorized note and version.
  Comparison currently uses the existing recording-scoped
  `GET /api/notes?file_id=…` and selects the note by ID.
- Return an explicit `created` flag from `POST …/editable-copy`. Currently both
  a new copy and an existing copy return 201. The frontend only adopts a copy
  whose content matches the loaded generated source and whose version is 1;
  changed content or any later revision enters recovery. Optimistic PUT protects
  against subsequent concurrent edits. An explicit flag would distinguish even a
  pre-existing untouched version-1 copy without inferring from its content/version.
- Provide structured rebuild error codes/processing status. The current route
  returns HTML. The frontend checks HTTP success, retains the dialog and saved
  Custom chip on failure, and shows deliberate failure/uncertain-result guidance
  instead of inserting proxy error HTML or announcing that work started.

All 17 recovery messages and three rebuild failure messages are translated in
the centralized Traditional Chinese catalogs (`i18n_ext/note_editor.py` and
`i18n_ext/workspace.py`). The frontend catalog test extracts the recovery/action
and rebuild failure strings from the production scripts and verifies that they
reach the merged browser catalog and translator without an English fallback.

The fixture isolates frontend behavior. The Python tests verify the existing
route/template/version contracts; this fixture does not run an actual worker or
claim to exercise the complete app shell, history restoration, or account switching.
