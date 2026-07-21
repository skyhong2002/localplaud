---
name: polish
description: One timeboxed product-polish iteration — drive a real user journey in the browser like a user, compare with Plaud Web, log gaps to the backlog, fix the top item, verify, commit, deploy.
---

# Polish loop

One invocation = ONE timeboxed iteration (~10 minutes of wall-clock work). Either
fix the top backlog item, or run one discovery sweep. Never both, never more.

## State

- Backlog: `.agents/polish/backlog.md` (create from the template at the bottom if
  missing). It holds the journey rotation pointer and a ranked list of findings.
- Every finding is one line: `- [ ] (P1|P2|P3) <journey>: <concrete symptom> — <evidence>`.
  Mark fixed items `- [x] … (commit <sha>)`. Never delete history.

## Decide the mode

1. Read `.agents/polish/backlog.md` and `CLAUDE.md`.
2. If an unchecked P1/P2 item exists → **fix mode** on the top item.
3. Otherwise → **discovery mode** on the next journey in the rotation.

## Discovery mode

Pick the next journey from the rotation list in the backlog, advance the pointer.

Drive the REAL app in a REAL browser via the `codex-computer-use` skill:
- Target: `http://127.0.0.1:8080` (login password = `LOCALPLAUD_API__LOGIN_PASSWORD`
  in `.env`; never print it). Desktop ~1400x900 AND mobile ~390px.
- Prompt Codex with a **user-behavior script, not a feature checklist**. Include
  disruptive, human moves: scroll while things are playing or loading, press
  Space/Escape/Tab/arrows, double-click, resize mid-action, click rapidly, go
  back and forward, reload mid-flow, try to do the task the "wrong" way first.
- When a matching reference flow exists on https://web.plaud.ai, run the same
  journey there READ-ONLY for comparison (see Guardrails) and collect concrete
  visual/interaction differences.

Append every gap to the backlog, ranked: P1 = broken/blocking, P2 = clearly worse
than Plaud or confusing, P3 = cosmetic. Pure taste calls (layout preferences with
no objective winner) get logged as `(taste)` proposals for the user instead of
being acted on.

## Fix mode

- Keep the diff small and single-purpose. If the fix looks bigger than ~30 minutes,
  split it into backlog subtasks and stop the iteration there.
- Jinja templates hot-reload in prod; any `.py` change needs
  `launchctl kickstart -k gui/$(id -u)/com.localplaud.agent` after editing.
- Verify the fix by DRIVING the affected flow in the real browser (codex-computer-use),
  including the user-behavior moves above — not just a curl or unit test.
- Run the related pytest module(s) with `env -u FORCE_COLOR NO_COLOR=1`.
- Commit with a descriptive message, mark the backlog item `[x]` with the sha.
  Prod serves from this checkout, so commit + (kickstart if Python changed) = deployed.

## Guardrails

- Plaud Web is read-only product research: NEVER press Generate, send an Ask
  prompt, create a share link, export, edit AutoFlows or settings, or modify
  account data. Never record private recording content in repo files.
- Respect CLAUDE.md product principles, especially provenance: Plaud-imported
  artifacts stay opt-in and visibly labelled everywhere.
- Keep tests green. Never commit secrets. Never leave the service down.

## Report

End with: mode, what was found or fixed (with evidence/screenshot paths), backlog
delta (new items / items closed), and the single most valuable next item.

## Backlog template

```markdown
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
```
