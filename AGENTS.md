# localplaud agent guide

## Product mission

localplaud is a self-hosted replacement for the **Plaud Intelligence subscription
workflow**, not merely a backup viewer and not a replacement for the recorder.

The only Plaud-managed path that remains in the normal workflow is:

```text
Plaud recorder -> Plaud App -> Plaud cloud raw-audio upload
```

After upload, localplaud must independently perform everything else:

```text
discover -> download raw audio -> transcode -> ASR -> align -> diarize
         -> vocabulary/name correction -> summaries/notes/mind map
         -> index -> single-file and whole-library Ask -> edit/export/automate
```

The user should be able to remain on Plaud's free plan and never press Plaud's
Generate button. Plaud-generated transcripts, summaries, outlines, embeddings,
or other paid Intelligence artifacts are not valid dependencies of the primary
pipeline.

## Non-negotiable product principles

1. **Raw audio is the boundary.** Plaud is the source of truth for device-uploaded
   audio and minimal recording metadata only. localplaud owns all derived artifacts.
2. **Subscription independence is testable.** A clean recording with no Plaud
   transcript or summary must complete every enabled localplaud stage and be fully
   usable in the Web App.
3. **Cloud artifacts are migration/debug input only.** If support for importing
   Plaud transcripts or summaries remains, it must be explicitly opt-in, visibly
   labelled, provenance-preserving, and excluded from independence acceptance tests.
   Never silently prefer it.
4. **Plaud-like means workflow parity, not pixel copying.** Match the successful
   information architecture and interaction model: recordings library, detail
   workspace, synchronized player/transcript, editable speakers and text,
   multidimensional notes, Ask, mind map, export, organization, and automation.
   Use original localplaud branding and assets; do not copy proprietary artwork.
5. **Every stage is durable and independently resumable.** A failure in embeddings
   must not discard or mark an otherwise valid transcript and summary as unusable.
   Store stage-level status, error, provider, model, version, and timestamps.
6. **The original audio and user edits are never destroyed.** Reprocessing creates
   traceable revisions or replaces only explicitly selected derived artifacts.

The product and Web App acceptance specification is
[`docs/product-workflow.md`](docs/product-workflow.md). Treat it as the source of
truth when implementation details conflict with older prose.

## Target user workflow

1. The user records with a physical Plaud device.
2. The official Plaud App uploads the recording. localplaud does not replace
   Bluetooth/Wi-Fi device transfer in this phase.
3. The poller detects the recording through Plaud's official read-only Open API and
   downloads its raw audio. No Plaud AI generation is required.
4. localplaud automatically processes the file with the configured rule/profile.
5. The user receives a ready recording in the localplaud Web App and can listen,
   correct, rename speakers, regenerate notes, ask questions, organize, and export.
6. Corrections become the canonical local transcript and flow into newly generated
   summaries, mind maps, search results, and answers.

The default experience must not require CLI use after initial installation and OAuth.

## ASR and speaker pipeline

The quality baseline is **OpenAI Whisper large-v3-turbo** (often exposed as
`turbo`), accelerated appropriately for each machine:

- Apple Silicon: `mlx-community/whisper-large-v3-turbo` through MLX Whisper.
- NVIDIA/CUDA and CPU: `large-v3-turbo`/`turbo` through faster-whisper or an
  equivalent CTranslate2 conversion that is verified against the same model family.

Whisper does **not** identify speakers by itself. Do not describe turbo as a
speaker-aware ASR model. The required speech pipeline is:

```text
VAD -> Whisper large-v3-turbo ASR -> word-level alignment
    -> speaker diarization -> assign speakers to words/segments
```

Use a current, production-quality pyannote diarization pipeline (or a benchmarked
equivalent). WhisperX is a valid integration pattern for alignment plus assignment.
The stored transcript must support word/segment timestamps, stable speaker IDs,
speaker display names, confidence/provenance where available, and later speaker
renaming. Diarization is part of the default quality path, not an optional cosmetic
stage. If it is unavailable, surface a clear degraded state rather than pretending
the transcript is complete.

Taiwan Mandarin and mixed Mandarin/English recordings are primary evaluation cases.
Benchmark accuracy, speaker error, hallucinations, timestamps, speed, and memory on
real user-owned recordings before changing the production default.

## Summaries and knowledge workflow

- Never truncate the tail of a long transcript. Use chunked/map-reduce or another
  hierarchical strategy with full-transcript coverage.
- Support auto selection and per-file selection of language, model, and templates.
- Templates are user-manageable data, not five hard-coded prompts forever.
- Multiple note outputs may coexist for one recording. Preserve provenance and
  revisions.
- Ask must support both one recording and the whole library, cite recordings, and
  link answers to playable timestamps.
- Index the corrected canonical transcript. Re-index after transcript or speaker
  edits without rerunning ASR.

## Web App product bar

The Web App is the primary product surface. It should feel as complete and immediate
as Plaud Web while remaining recognizably localplaud.

Required areas include:

- library navigation with search, folders/tags, filters, processing state, and
  responsive list/detail layouts;
- a recording workspace with synchronized audio, clickable active transcript,
  speaker colors/naming, inline edits, summaries, mind map, and single-file Ask;
- whole-library Ask with grounded citations and source navigation;
- generation/re-generation controls, template selection, visible progress, and
  actionable failure recovery;
- export/share controls with practical audio, transcript, subtitle, note, image,
  and document formats;
- settings for sync, ASR, diarization, vocabulary, LLMs, embeddings, automation,
  privacy, and system health.

HTMX/Jinja may continue where it supports this experience. It is not a product
constraint: adopt richer client-side state or a SPA architecture if synchronized
audio, editing, optimistic updates, navigation, or accessibility materially benefit.

## Architecture and data ownership

- `poller`: read-only Plaud listing and raw-audio download.
- `store`: original audio on disk; metadata, revisions, artifacts, stage runs, and
  embeddings in the local database.
- `worker`: durable staged processing and reprocessing.
- `api/ui`: complete daily-use Web App plus a documented API.

Derived artifacts must record at least `source`, provider/model, configuration or
prompt version, creation time, and revision relationship. The default source is
`local`; Plaud-produced data must never be ambiguously labelled as local.

SQLite remains suitable for a single-user deployment, but schema and worker design
must not assume one monolithic `status` value is enough. Retrieval can scale beyond
brute-force vectors when library size requires it.

## Plaud API rules

- Prefer the official OAuth Open API. The current verified read-only endpoints and
  legacy enrichment notes live in [`docs/plaud-api.md`](docs/plaud-api.md).
- Only access the authenticated user's own account and data.
- Keep normal operation read-only against Plaud. Do not add cloud mutations without
  explicit user authorization and a separate design review.
- Treat signed download URLs as short-lived and SSRF-check external fetches.
- Do not make availability of Plaud transcript/summary fields a pipeline prerequisite.

## Working rules for coding agents

- Read this file and `docs/product-workflow.md` before making product, pipeline, or
  Web App changes.
- The repository is implemented and deployed; do not treat it as a blank project.
- Diagnose the real running path and distinguish warnings from blockers.
- Preserve user-owned worktree changes. Never commit secrets or data.
- For behavior changes, add tests around subscription independence, provenance,
  resumability, and the affected user journey.
- For Web App changes, verify the rendered result in a real browser at desktop and
  mobile widths, including empty, loading, degraded, error, and long-content states.
- Keep README, configuration examples, ADRs, deployment docs, and TODO status aligned
  with implementation. Label targets as targets; do not claim unfinished behavior is
  already working.
- User-facing UI, configuration, exports, and failure messages should be deliberate,
  consistent, and accessible.

## Definition of done for subscription replacement

A feature set is not complete until a newly uploaded raw recording can, without any
Plaud Intelligence artifact:

1. automatically reach usable local completion;
2. produce a timestamped, diarized, editable transcript;
3. produce full-coverage notes and a mind map;
4. appear in both single-file and cross-library Ask with playable citations;
5. survive provider/stage failures and resume without unnecessary recomputation;
6. export in the promised formats; and
7. be operated from the Web App after initial setup.
