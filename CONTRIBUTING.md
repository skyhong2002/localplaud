# Contributing to localplaud

Help make recordings easier to understand, correct, and use—without depending on
Plaud Intelligence artifacts. Contributions to code, documentation, accessibility,
translations, and reproducible bug reports are welcome.

Read the [product workflow](docs/product-workflow.md) and [agent guide](AGENTS.md)
before changing product behavior. The repository is an existing application;
the product specification distinguishes implemented behavior from targets.

## Start a conversation

- Use [Discussions](https://github.com/skyhong2002/localplaud/discussions) for setup
  questions, workflow ideas, and feedback.
- Use [Issues](https://github.com/skyhong2002/localplaud/issues/new/choose) for
  reproducible bugs and concrete feature requests. Check existing reports first.
- Report vulnerabilities through the [private security channel](SECURITY.md).
- For substantial architecture or workflow changes, discuss the proposal before
  implementing it so scope and compatibility can be agreed on.

Follow the [Code of Conduct](CODE_OF_CONDUCT.md) in every project space.

## Set up a development environment

```bash
git clone https://github.com/skyhong2002/localplaud.git
cd localplaud
uv sync --extra dev --extra cloud
uv pip install "torch>=2.8"
```

The full suite uses cloud SDKs with mocked transports and PyTorch for synthetic
audio-feature tests; it does not need provider credentials or model weights.
CI installs the CPU PyTorch build. Use Python 3.11 or newer. The main CI
matrix covers Python 3.11 and 3.12. Install
ffmpeg and the relevant optional extras when working on media or model runtimes;
see the [quickstart](README.md#quickstart). Keep private configuration, tokens,
recordings, and model downloads out of Git.

## Make and verify a change

Create a branch with a focused change. Describe the user-visible problem and the
resulting behavior. Update configuration examples and documentation when behavior
changes; label planned work as planned.

```bash
uv run --no-sync ruff check src tests
uv run --no-sync pytest -q
```

Tests must use synthetic fixtures and temporary data. Do not run them against a
live library, Plaud account, or paid provider. For isolated runtime checks with
inherited deployment settings removed and outbound sockets blocked:

```bash
LOCALPLAUD_TEST_PYTHON="$PWD/.venv/bin/python" sh tests/run_runtime_offline.sh
```

Pass test paths after the script name to run a specific suite. For behavior
changes, cover the affected journey, subscription independence, provenance, and
failure/resume behavior. Web App changes also need real-browser verification at
desktop and mobile widths, including empty, loading, degraded, error, and long
content states. See [UI verification](tests/ui/README.md).

## Preserve the product boundaries

- Derive the primary workflow from raw audio and minimal metadata. Plaud AI
  imports must stay opt-in, labelled, and outside independence acceptance tests.
- Preserve original audio and user edits. Reprocessing must retain traceable
  revisions and allow independent stage recovery.
- Select providers per stage. Never introduce an implicit cloud fallback.
- Keep long-transcript coverage complete and citations tied to their sources.
- Treat Plaud product research as read-only. Do not generate, share, edit,
  export, or delete account data as part of an audit without explicit permission.

## Open a pull request

Explain the concrete trigger, the behavior before and after, and the checks you
ran. Include synthetic screenshots for UI changes and call out migrations,
provider egress, new dependencies, or compatibility limits when relevant. Do not
include private recording content, account identifiers, credentials, or signed
download URLs in issues, screenshots, logs, or commits.

Contributions are distributed under the project's [MIT license](LICENSE).
