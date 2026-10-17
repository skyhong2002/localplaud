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

The fixture loads the production note editor, workspace JavaScript, and CSS.
Its note/form selectors match the workspace template. It intercepts every
JavaScript API request with a closed allowlist, generates a silent WAV in memory,
and uses no app state, accounts, private media, worker, model, or external service.
Download checks intercept the click and inspect the generated Blob; they do not
write files. Clipboard tests replace the clipboard API and deliberately deny the
fallback. The fixture overrides confirmation dialogs to retain unsaved drafts.

The scenarios cover:

- 409 and ExistingCopy responses with additional typing during the request,
  exact serialized Markdown recovery, retained DOM, no subsequent writes, and
  native/HTMX navigation protection;
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
390×844: **13/13 scenarios passed at each size**. The recovery comparison was
also visually inspected at both widths. JavaScript syntax checks passed.
The existing isolated Python suites for manual notes UI, user note history,
speech overrides, web UI, and modal visibility passed: **83 tests**.

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
HTMX navigation targeting `#app-view` requests explicit discard confirmation.
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
- Add the new `tr(...)` recovery/rebuild messages to the centralized Traditional
  Chinese catalog (`i18n_ext/note_editor.py` / workspace catalog). This task owns
  frontend assets/tests/docs only; new messages currently use the English fallback.

The fixture isolates frontend behavior. The Python tests verify the existing
route/template/version contracts; this fixture does not run an actual worker or
claim to exercise the complete app shell, history restoration, or account switching.
