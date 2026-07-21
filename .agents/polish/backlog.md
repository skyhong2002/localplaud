# Polish backlog

Rotation pointer: 1

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

- [ ] (P3) Share: Plaud's transcript view has a right-edge minimap (small dashes indicating scroll position); localplaud share page has a plain scrollbar — 2026-07-22 parity audit
- [ ] (P3) Detail: app player (canvas waveform) vs Plaud share player (flat scrubber) diverge; consider one shared player component/style
- [ ] (taste) Share: summary tab shows every note's own heading (Summary, Outline); Plaud renders one continuous document — propose merging or ordering controls to the user
