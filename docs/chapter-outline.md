# Timestamped chapter outlines

Chapter outlines are local derived artifacts, separate from notes and mind maps.
They always read the complete corrected canonical **local** transcript. Plaud
transcripts, summaries and outlines are never inputs. Earlier outline revisions,
original audio, ASR and user edits remain intact when an outline is regenerated.

Automatic generation is opt-in: `[pipeline] outline = false` is the default.
`outline_method = "llm"` uses the durable mind-map text-model selection, including
its explicit fallbacks, no-egress policy and shared cost ledger. Reservations cover
both allowed validation attempts per part and consolidation. Remote workers advertising the `outline` capability and selected model can execute
the LLM method. Older workers fail explicitly before submission; only the configured
mind-map fallback order can change target. The controller verifies SHA-256, exact
model/prompt version, contiguous full coverage and segment-boundary playback starts.
Job keys include canonical input hashes, selection, duration and prompt version.
`time_slices` is a separately selected local method with no model or egress: fixed
time windows, titled from the first substantive sentence. It is not a topic model
and never substitutes silently for a failed LLM call.

The LLM receives all timestamped segments in ordered parts. It may consolidate
adjacent chapter titles, but cannot invent playback positions: all starts are valid
segment boundaries (the first is zero), chapter ranges are contiguous, and the tail
is covered. Non-finite, reversed, unordered, or out-of-recording timestamps fail
before generation. Single oversized segments are preserved whole; a provider
context-limit error is reported rather than truncating their text.

Each generation records the canonical transcript id/revision/source, input digest,
provider/model, method/prompt version, profile snapshot, creation time and monotonically
increasing outline revision. Transcript or speaker edits mark the stage stale;
publication also checks the current canonical digest so concurrent old generations
cannot overwrite current inputs. A failed rebuild retains the last artifact and
fails only the outline stage. Explicit requests persist their method and pending
state before acknowledgement. Interrupted requests and failed attempts resume at
outline scope through the normal worker with bounded backoff, including a crash
after the provider attempt has started and a rebuild invoked directly by a worker; no ASR, notes or
indexing is repeated by that narrow retry. Automatic processing must be enabled
(or the normal worker invoked) for retries to run.

## API

- `GET /api/files/{id}/outline`: `status` (`missing`, `pending`, `running`,
  `completed`, `failed`, `skipped`), `stale`, `error`, `method`, `chapters`,
  `provenance`, `feedback`, `duration_ms`, and `generation`.
- Each chapter is `{start_ms, end_ms, title}`. Divide milliseconds by 1,000 for
  playback. The last successful artifact remains in `chapters` during rebuilds
  and after failure; the client must show `stale` and current status explicitly.
- `generation.selection` and `.fallbacks` expose only `provider_type`, `model`,
  `execution_target`, `data_egress`. `llm_available` checks profile resolution and
  supported target, not runtime health or quota. `cost_ceiling_usd` is the profile
  ceiling, not a price quote. No provider configuration or secrets are returned.
- `POST /api/files/{id}/outline/regenerate` accepts
  `{ "method": "llm" | "time_slices" }`; omission uses the configured method.
  A valid request returns 202 after persisting its queue; a competing recording
  claim or missing local transcript returns 409. The worker records failures for
  the read endpoint. If its thread cannot start, the durable request remains queued.
- `POST /api/files/{id}/outline/feedback` accepts `{ "value": "up" | "down" | null }`.
  Feedback stays local on the current outline revision.

The additive `outlines` table is created by normal database initialization. It does
not rewrite existing transcripts or summaries. The test suite exercises adding it
to a populated pre-outline database and initializing repeatedly. Real model calls
and deployment remain separate operational validation; deterministic and fake-model
regressions do not claim transcription or topic-quality benchmarks.
