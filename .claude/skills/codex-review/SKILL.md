---
name: codex-review
description: Ask the Codex CLI (GPT-5.6) for an independent code review of uncommitted changes, a branch diff, a commit, or a specific implementation. Use when the user wants a second-pass review, an independent perspective, or when a change is broad enough that another reviewer is useful. Codex is the independent reviewer; Claude verifies its claims before presenting them.
---

# Codex review

Codex (GPT-5.6) acts as an independent reviewer. It is smart but has no taste — trust it on correctness, logic, and missed edge cases; be skeptical of its style and API-design opinions.

This skill is usually run by a subagent that triggers Codex, collects the report, and passes it back up to the parent.

## Workflow

1. **Identify the review target** — uncommitted changes, a branch diff against a base, a specific commit, or a described implementation area.
2. **Create a temporary artifact directory** for the Codex report (use the session scratchpad).
3. **Run Codex** with a focused review prompt (commands below).
4. **Read the report and verify the important claims against the actual code** before presenting them. Never forward Codex findings unverified.

## Commands

Use plain `codex` on PATH — the npm global install (0.144.0). If it's missing or too old for the configured GPT-5.6 model ("requires a newer version of Codex"), fall back to the ChatGPT desktop app's bundled binary `/Applications/ChatGPT.app/Contents/Resources/codex`.

When running outside a git repository, add `--skip-git-repo-check`.

Built-in review mode (preferred when it fits):

```bash
# Review staged, unstaged, and untracked changes
codex exec review --uncommitted "<optional focus instructions>" -o <report-file>

# Review a branch against a base
codex exec review --base main "<optional focus instructions>" -o <report-file>

# Review a specific commit
codex exec review --commit <sha> "<optional focus instructions>" -o <report-file>
```

General-purpose read-only review (for "review this module/implementation" requests that aren't a diff):

```bash
codex exec -s read-only -C <repo-root> -o <report-file> "<self-contained prompt>"
```

Codex runs can take several minutes — use a generous timeout or run in background. `-o` writes the final message to a file so nothing is lost.

## Prompting Codex

Prompt Codex simply. It is not Claude — it does not need role framing, guardrails, or elaborate structure, and it won't do things you didn't ask for. A good prompt looks like:

> Review the diff for correctness bugs, race conditions, and missed edge cases. Focus on src/sync/. Report file:line for each finding.

## Reporting results

- If Codex finds nothing, say that clearly and state what review target it inspected. Do not treat an empty finding list as a failure and do not rerun.
- Present findings with your own verification verdict attached (confirmed / could not confirm), most severe first.
