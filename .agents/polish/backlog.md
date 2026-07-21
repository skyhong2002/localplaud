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

- [ ] (P1) Library table: mobile sort and select-all controls overlap the first card, and tapping the apparent header checkbox selects only the first recording instead of all visible recordings — 2026-07-22 browser audit at 390px; `/tmp/localplaud-polish-20260722/discovery/P1-mobile-select-all-overlaps-first-row-390x844.png`
- [ ] (P1) Library table: destructive bulk processing deletion is presented in the same menu with the same Apply button as routine folder/tag actions, with no inline warning or visual separation — 2026-07-22 browser audit; `/tmp/localplaud-polish-20260722/discovery/P1-desktop-bulk-delete-option-adjacent-1400x900.png`
- [ ] (P2) Library table: an empty folder/tag manager is dead-ended because it reports no items but offers no creation action inside the dialog — 2026-07-22 browser audit at desktop and mobile widths
- [ ] (P2) Library table: active filters are represented only by a count badge, and an empty filtered result offers no visible filter summary or reset action — 2026-07-22 browser audit; `/tmp/localplaud-polish-20260722/discovery/P2-desktop-filter-empty-no-visible-reset-1400x900.png`
- [ ] (P2) Library table: pagination exposes only previous/next and silently clears page-scoped bulk selection after navigation — 2026-07-22 browser audit across 779 recordings at desktop and mobile widths
- [ ] (P3) Library table: the mobile drawer uses a nested scroll region whose thin scrollbar is the only cue that more sources and workspace links are available — 2026-07-22 browser audit at 390px
- [ ] (P3) Share: Plaud's transcript view has a right-edge minimap (small dashes indicating scroll position); localplaud share page has a plain scrollbar — 2026-07-22 parity audit
- [ ] (P3) Detail: app player (canvas waveform) vs Plaud share player (flat scrubber) diverge; consider one shared player component/style
- [ ] (taste) Share: summary tab shows every note's own heading (Summary, Outline); Plaud renders one continuous document — propose merging or ordering controls to the user
