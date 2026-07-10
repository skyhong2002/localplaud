# ADR 0001: Tech stack — Python, Typer CLI, SQLAlchemy/SQLite, FastAPI + HTMX

Status: Accepted

## Context

localplaud downloads Plaud raw recordings and independently replaces the paid
Intelligence workflow: polling an HTTP API, running ASR/alignment/diarization,
calling LLMs, storing editable derived artifacts, and serving a full daily-use
Web App. The dominant backend constraint is the
ASR/diarization ecosystem — faster-whisper, whisper.cpp bindings,
mlx-whisper, pyannote.audio, sentence-transformers — which is
overwhelmingly Python. The tool must run on a Mac mini and in Linux
Docker, be installable by one person, and need no build pipeline.

## Decision

- **Python 3.11+** as the sole implementation language; **uv** for
  environment and dependency management.
- **Typer + Rich** for the CLI (`localplaud` entrypoint) — typed
  subcommands, good help text, readable progress output.
- **httpx** for all HTTP (Plaud API client, cloud providers): sync and
  async with one API, HTTP/2, sane timeouts.
- **pydantic + pydantic-settings** for configuration: typed sub-models,
  layered sources — defaults → `config.toml` → `.env`/environment with
  the `LOCALPLAUD_` prefix and `__` nesting (see `src/localplaud/config.py`).
- **SQLAlchemy 2 (typed ORM) + SQLite** to start. The engine is created
  from `store.database_url`, so Postgres is a config swap, not a rewrite.
- **FastAPI** remains the application/API server. **Jinja2 + HTMX** are the
  initial rendering stack and remain appropriate for server-driven areas.
  They are an implementation starting point, not a product constraint: richer
  client-side state or a SPA may be introduced for synchronized playback,
  inline editing, optimistic updates, accessible navigation, and other
  interactions required by `docs/product-workflow.md`.

## Consequences

- ASR/diarization/embedding libraries plug in directly; heavy ones stay
  behind optional extras so the core install is light.
- Single language across poller, worker, and UI; one packaging story
  (hatchling wheel, `localplaud` console script).
- SQLite means zero-ops single-user storage; concurrency is bounded, which
  matches the pipeline's `concurrency = 1` default. Postgres remains the
  documented scale-up path via `database_url`.
- The primary Web App must approach Plaud's workflow completeness and responsiveness.
  Architecture is evaluated against that experience rather than minimizing frontend
  tooling at all costs. A frontend architecture change should still be deliberate,
  incremental, tested, and documented.
- Python-level performance is acceptable because the hot paths (ASR
  inference, ffmpeg) run in native code anyway.
