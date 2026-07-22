# Polish backlog

Rotation pointer: 2

Journeys:
1. Library table — filters, folders, tags, sort, bulk select, pagination
2. Recording detail — tabs, player/transcript sync, speaker rename, inline edits
3. Share — dialog options, public page desktop+mobile, player UX
4. Ask — single file and library-wide, citations, history drawer
5. Search — queries, result navigation, empty states
6. Templates & Discover — browse, install, generate with template
7. Settings — every pane, save/error states
8. Import flows — local upload, Plaud metadata import, progress states
9. Mobile — drawer nav, all of the above at 390px
10. Keyboard & a11y — focus order, shortcuts, Escape/Tab traps, screen-reader labels
11. Degraded states — recordings with no audio / no transcript / failed stages

## Findings

- [x] (P1) Library table: mobile sort and select-all controls overlap the first card, and tapping the apparent header checkbox selects only the first recording instead of all visible recordings — 2026-07-22 browser audit at 390px; `/tmp/localplaud-polish-20260722/discovery/P1-mobile-select-all-overlaps-first-row-390x844.png` (commit 146eaab)
- [x] (P1) Library table: destructive bulk processing deletion is presented in the same menu with the same Apply button as routine folder/tag actions, with no inline warning or visual separation — 2026-07-22 browser audit; `/tmp/localplaud-polish-20260722/discovery/P1-desktop-bulk-delete-option-adjacent-1400x900.png` (commit c5f7a2a)
- [x] (P2) Library table: an empty folder/tag manager is dead-ended because it reports no items but offers no creation action inside the dialog — 2026-07-22 browser audit at desktop and mobile widths; fixed and reverified at 1400x900 and 390x844 in `/tmp/localplaud-polish-20260722/folder-tag-manager/` (commit c5fb973)
- [x] (P2) Library table: deleting the currently filtered folder or tag can leave or resurrect a stale selected filter through browser history, producing an empty library for a now-missing organization ID — fixed and reverified at 1400x900 and 390x844 in `/tmp/localplaud-polish-20260722/stale-history/` (commit 10879b2)
- [x] (P2) Library table: active filters are represented only by a count badge, and an empty filtered result offers no visible filter summary or reset action — fixed and reverified at 1400x900 and 390x844 in `/tmp/localplaud-polish-20260722/active-filter-empty/` (commit 70b0b8c)
- [x] (P2) Library table: pagination exposes only previous/next and silently clears page-scoped bulk selection after navigation — fixed and reverified at 1400x900 and 390x844 in `/tmp/localplaud-polish-20260722/pagination-selection/` (commit bddd522)
- [x] (P2) Keyboard & a11y: preserved-data chips in the bulk cleanup warning are generic spans rather than a semantic list, so assistive technology cannot announce them as a grouped set — fixed and reverified at 1400x900 and 390x844 in `/tmp/localplaud-polish-20260722/semantic-list/` (commit 964aa3f)
- [x] (P2) Library table: the Traditional Chinese bulk toolbar still renders `No folder` in English — fixed and reverified at 1400x900 and 390x844, including reload and browser history, in `/tmp/localplaud-polish-20260722/no-folder-localization/` (commit a99aac1)
- [x] (P2) Library table: Traditional Chinese source navigation and the active-filter summary render numbered capture sources as English `Source 1` / `Capture source 1` — fixed and reverified on the live service at 1400x900 and 390x844, including filter navigation, drawer scrolling, reload, and browser history, in `/tmp/localplaud-polish-20260722/capture-source-labels/live/` (commit 54fb467)
- [x] (P2) Keyboard & a11y: the Traditional Chinese bulk toolbar retains English accessible names for its action, target, select-all, and row-selection controls — routed all four aria-labels through t() + added catalog entries; verified by new render test `test_bulk_toolbar_accessible_names_are_localized` asserting 整理動作 / 資料夾或標籤 / 選取本頁所有錄音 / 選取錄音 render with no English leak (commit 2f6ede3)
- [x] (P3) Keyboard & a11y: literal English aria-labels remain in the TC shell outside the bulk toolbar — `notifications.html` Dismiss + read-toggle (Mark read/unread), `settings.html` "Settings sections" nav, and `index.html` orgbox "Library organization" — routed all through t() with catalog entries; verified by render tests on notifications/settings/library in zh-Hant-TW (commit 3bf2254)
- [x] (P2) Library table: the Library route accepts and summarizes a text query alongside source filters, but its `.library-search` form is hidden at every viewport, so users cannot create that combined filter state through the visible Library UI — surfaced the form as a full-width `role=search` row under the header with filters riding along as hidden fields; verified by new render test `test_library_search_box_is_visible_and_preserves_filters` (commit c5a919b). NOTE: live-browser layout re-check still pending — codex computer-use hung this session; re-verify desktop+mobile visual placement when a browser is available.
- [ ] (P3) Library table: at 1400px the bulk toolbar wraps `清除` alone onto a second row despite ample horizontal space — 2026-07-22 browser verification; `/tmp/localplaud-polish-20260722/no-folder-localization/desktop-no-folder-1400x900.png`
- [ ] (P3) Library table: the mobile drawer uses a nested scroll region whose thin scrollbar is the only cue that more sources and workspace links are available — 2026-07-22 browser audit at 390px
- [ ] (P3) Mobile: resizing from desktop to 390px with an active bulk selection can briefly leave the desktop sidebar clipped over the library before the responsive layout settles — 2026-07-22 disruptive browser verification; `/tmp/localplaud-polish-20260722/pagination-selection/mobile-01-selection-before-page-change.png`
- [x] (P3) Keyboard & a11y: blank folder/tag creation uses the browser's English `Please fill out this field.` validation bubble inside the Traditional Chinese UI — localized the native bubble via setCustomValidity('Enter a name') on the create-form name inputs, keeping required for a11y; wiring covered by `test_index_page_renders_table_and_controls` (commit d04609a). NOTE: native-bubble render is JS-only; visually re-confirm in a browser when available.
- [x] (P3) Library table: long folder names are ellipsized in the desktop sidebar without a tooltip or other full-name affordance — added `title="{{ folder.name }}"` to the sidebar folder anchor (matching source links / filter chips); verified by regression test `test_sidebar_folder_link_exposes_full_name_via_title` (commit da955f2)
- [ ] (P3) Library table: at 390px the folder/tag delete confirmation wraps Cancel and Delete onto separate rows and opposite sides, weakening their visual grouping — 2026-07-22 browser verification; `/tmp/localplaud-polish-20260722/stale-history/mobile-delete-confirm-390x844.png`
- [ ] (P3) Library table: the bulk cleanup warning repeats Plaud-import and Saved-note preservation details in its explanatory paragraph — 2026-07-22 browser audit; `/tmp/localplaud-polish-20260722/bulk-delete-separation/mobile-warning-final-390x844.png`
- [x] (P3) Shell: the live browser requests `/favicon.ico` but the service returns 404, leaving the browser tab without the expected site icon — added a public `/favicon.ico` route serving the existing SVG; verified on the live service (200 image/svg+xml, home still 401) and by regression test `test_favicon_is_public_and_served` (commit 7e6d995)
- [x] (P3) Share: the dynamic-action localization guard misclassifies the technical `${speeds[speedIndex]}x` playback-speed display as untranslated prose, leaving that test red — added it to the guard's `technical_fragments`; full suite (840) now green (commit aa9a98b)
- [ ] (P3) Share: Plaud's transcript view has a right-edge minimap (small dashes indicating scroll position); localplaud share page has a plain scrollbar — 2026-07-22 parity audit
- [ ] (P3) Detail: app player (canvas waveform) vs Plaud share player (flat scrubber) diverge; consider one shared player component/style
- [ ] (taste) Share: summary tab shows every note's own heading (Summary, Outline); Plaud renders one continuous document — propose merging or ordering controls to the user
