---
name: codex-implementation
description: Delegate bounded, well-specified implementation work to the Codex CLI (GPT-5.6), usually in a git worktree. Use for bulk mechanical work — clear-spec implementations, migrations, data analysis scripts, large refactors with an obvious pattern — where GPT-5.6's effectively-free usage saves significant tokens. Not for user-facing API design, UI, or copy (taste-sensitive work stays with Claude models).
---

# Codex implementation

GPT-5.6 via Codex is effectively free and very capable at bounded implementation work. Use it to burn through mechanical tasks; keep taste-sensitive decisions (API shape, UI, copy, public SDK surface) with Claude models.

## When to use

- The spec is clear and self-contained — Codex should not need to make product or design decisions.
- The work is token-heavy: big migrations, repetitive edits, generated code, data analysis.
- The work can be verified after the fact (tests, typecheck, diff review).

## Workflow

1. **Isolate the work.** Create a git worktree (or use the existing one if already isolated) so Codex's changes can't disturb other work:
   ```bash
   git worktree add <scratch-path> -b codex/<task-slug>
   ```
2. **Write a self-contained prompt.** Include: the task, relevant file paths, constraints, and how to verify (e.g. "run `pnpm test` and make it pass"). Codex has no context from this conversation — everything it needs must be in the prompt.
3. **Run Codex with write access in that directory:**
   ```bash
   codex exec -s workspace-write -C <worktree-path> -o <report-file> "<prompt>"
   ```
   Outside a git repository, add `--skip-git-repo-check`.
   Long tasks: run in background with a generous timeout. `-o` preserves the final message.
4. **Verify the results yourself.** Read the diff (`git -C <worktree-path> diff`), run the tests, and check the work against the spec before reporting it done or merging it anywhere.
5. **Report back** with what changed, verification results, and the worktree/branch location.

## Prompting Codex

Keep prompts short and direct. State the task, the paths, the constraints, and the expected deliverable. Do not prompt it like Claude — no role-play, no elaborate guardrails. Codex won't do things you didn't ask for.

If Codex reports it could not complete the task, pass that up honestly with its stated reason — do not silently retry more than once.
