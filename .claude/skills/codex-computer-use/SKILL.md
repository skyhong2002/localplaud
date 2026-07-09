---
name: codex-computer-use
description: Ask the Codex CLI (GPT-5.6) to run local app verification that needs computer use, browser automation, simulators, screenshots, app launching, or independent runtime inspection. This is how GPT-5.6 is invoked for computer-use work. Use when the user asks Claude to test a flow, verify UI behavior, inspect a running app, capture screenshots, or report confirmation and feedback about implemented behavior that benefits from computer use.
---

# Codex computer use

Codex's computer use is currently far stronger than what's available natively here — it can drive the full Mac (native apps, Xcode, simulators), not just a browser tab. The `computer-use` plugin is enabled in this machine's Codex config, so `codex exec` sessions can control the desktop. Shell out to it for any verification that needs eyes and hands on a running app.

## Workflow

1. **Make sure the target is reachable.** If the app or dev server needs to be running, either start it first or tell Codex exactly how to launch it in the prompt.
2. **Create an artifact directory** in the session scratchpad for screenshots/recordings, and tell Codex to save evidence there.
3. **Run Codex:**
   ```bash
   codex exec -C <project-root> -o <report-file> "<simple, self-contained prompt>"
   ```
   Outside a git repository, add `--skip-git-repo-check`.
   Computer-use runs are long — screenshots and navigation take real time. Run in background with a generous timeout (10+ minutes is normal). `-o` preserves the final report.
4. **Read the report and the saved screenshots**, verify the claims that matter, and pass the confirmed results up.

## Prompting Codex

Simple and concrete. State: what app/flow to exercise, how to launch or reach it, what to check, and where to save screenshots. Example:

> Open the app at http://localhost:3000, log in with the test account in .env.test, go through the checkout flow, and verify the discount code field applies a 10% discount. Save a screenshot of each step to <artifact-dir>. Report what worked and what didn't with the matching screenshot filename.

Do not prompt it like Claude — no elaborate structure or guardrails needed.

## Reporting results

- If Codex confirms the behavior works, report that with the screenshot evidence paths.
- If it finds nothing wrong, say that clearly and state what flow it actually exercised — an empty finding list is a pass, not a failure to rerun.
- If it hits environment problems (app not running, login blocked), report the blocker rather than retrying blindly.
