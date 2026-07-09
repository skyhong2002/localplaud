# Model routing for workflows and subagents

> 從 `~/.claude/CLAUDE.md` 複製過來的全域 routing 規則（2026-07-10），讓這個專案在沒有全域設定的機器（Mac mini、Linux Docker）上也能用。改了全域版記得同步這份。

If computer use is helpful for completing or verifying work, shell out to GPT-5.6 with Codex (the `codex` CLI — call it via Bash).

## Glossary

- **Intelligence**: how hard of a problem the model can handle unsupervised.
- **Taste**: UI/UX, code quality, API design, and copy.

When I describe tasks or complaints using these words, this is what I mean, and this is how to apply the scores below.

## Model scores (1–10, higher = better for that axis)

| Model | Cost | Intelligence | Taste | Notes |
|---|---|---|---|---|
| GPT-5.6 (via Codex CLI) | 9 | 9 | 4 | My Codex subscription is extremely generous — treat GPT-5.6 as effectively free. Solves problems at any complexity and pattern-matches very well, but writes TypeScript like a Python dev and Rust like a paranoid C++ dev. Not the code I want in public-facing SDKs and APIs. |
| Sonnet 5 | 5 | 5 | 5 | Not much cheaper than Opus in practice (token-hungry), much less intelligent, slightly more taste than GPT. |
| Opus 4.8 | 4 | 7 | 8 | Often effectively cheaper than Sonnet 5 despite the price. Meaningfully more intelligent, way higher taste. |
| Fable 5 | 2 | 10 | 10 | Cost sucks. Intelligence and taste are best in class. |
| Haiku 4.5 | — | — | — | Never use Haiku. It's not useful for anything real, especially with GPT-5.6 being effectively free. |

## How to apply this

- These are defaults, not limits. You have standing permission to override them. If a cheaper model's output doesn't meet the bar, rerun or redo the work with a smarter model without asking. Judge the output, not the price tag. Escalating costs less than shipping mediocre work.
- Don't let cost prevent you from using the right model for the job. Instead, take advantage of cheaper options to get more information and try things before moving the work to a more expensive option.
- Bulk mechanical work — clear-spec implementations, data analysis, migrations, digging through logs, reading giant PDFs or implementation specs — goes to GPT-5.6. It's effectively free.
- Anything user-facing (UI, copy, API design) needs taste ≥ 7.
- Reviews of plans and implementations: Fable 5 or Opus 4.8. Optionally add GPT-5.6 as an extra independent perspective.

## Mechanics of calling GPT-5.6

- GPT-5.6 is only reachable through the Codex CLI. Use the `codex-implementation`, `codex-review`, and `codex-computer-use` skills (in this repo's `.claude/skills/`) for the work they cover.
- **Which binary**: plain `codex` on PATH — the npm global install (`npm install -g @openai/codex`). On the main Mac it lives at `~/.local/bin/codex`; the ChatGPT desktop app's bundled binary `/Applications/ChatGPT.app/Contents/Resources/codex` is the fallback if the PATH one is missing or errors with "requires a newer version of Codex". On other machines, verify `codex --version` works and is logged in (`codex login status`) before routing work to it — if it isn't set up, say so instead of silently doing the work on a Claude model.
- When running outside a git repository (e.g. in a scratchpad), add `--skip-git-repo-check` or Codex refuses to start.
- For work the skills don't cover (investigation, data analysis), run `codex exec -s read-only "<self-contained prompt>"` directly via Bash.
- Workflows can't call GPT-5.6 as a model option — workflow agents must be Claude models. To use GPT-5.6 inside a workflow or as a subagent, spawn a Sonnet agent on low effort whose only job is to invoke Codex via Bash, wait for it, and report the results back up.
- Prefix the label of any agent or workflow stage that shells out to Codex with `[codex]` so I can see at a glance which work is running on GPT-5.6.
- Codex tasks can run long and time out. Use generous Bash timeouts or `run_in_background`, and pass `-o <file>` (`--output-last-message`) so the final answer survives even if the stream is cut off.
- Prompt Codex simply and directly. Do not prompt it like it's Claude — it doesn't need guardrails, role-play, or elaborate structure, and it won't do things you didn't ask for. One short paragraph with the task, the paths, and the expected output format is ideal.
