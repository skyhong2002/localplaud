# GPT-6 text-stage configuration

The Codex subscription adapter defaults to `gpt-6.1-sol` with high reasoning effort.
It can be explicitly selected for `correct`, `summarize`, `mind_map`, and `ask`.
Ask keeps the existing single-recording/library retrieval boundary, playable
citations, profile provenance, and durable provider-cost reservations. All four
stages use the same ephemeral no-tools invocation and subscription reserve.
The adapter remains profile-scoped and requires explicit cloud egress.

GPT-6 is a text-generation selection, not an ASR, alignment, diarization or
embedding model. Keep those stages on models that advertise the required
capability. Existing recording artifacts and their historical model attribution
must not be relabelled when settings change.

For an authorized workspace migration:

1. Verify the requested model with the configured Codex login using synthetic
   input. Model-cache visibility and login health alone do not prove inference.
2. Back up configuration and the database. Pause automatic dispatch and let active
   work finish, or stop the daemon and recover its interrupted claims through
   the normal restart path, before changing provider selections.
3. Add the verified model to the capability catalog. Create immutable profile
   versions selecting it for all four text stages, with explicit cloud egress.
   Remove non-target text fallbacks when the user requests only GPT-6 execution.
4. Update the system default and recording, folder, template or automation
   selections that would otherwise override it. Preserve other stage selections,
   cost limits, old profiles and artifact provenance.
5. Validate resolved profiles, a grounded Ask response and quota failure behavior,
   then restore the previous automatic-processing preference.

Offline-named profiles must not silently become cloud profiles: preserve the old
version and give a new cloud-enabled version a name that describes its behavior.
Changing settings does not itself authorize regenerating existing artifacts.

Official model guidance: <https://developers.openai.com/api/docs/guides/latest-model>.

## Deployment verification (2026-09-23)

After explicit authorization, the configured Codex login passed synthetic GPT-6
Sol inference and grounded Ask checks. Ask retained a playable timestamp and
correctly distinguished a settled amount from an undecided date. The affected
configuration/provider/Ask/fallback/subscription-independence suite passed 98 tests.

The production migration created seven immutable profile versions, updated all
20 recording overrides and verified all 943 recording resolutions: correction,
summary, mind map and Ask select `gpt-6-sol` with no text-stage fallback to an older
model. Non-text stage selections, existing transcript/summary content, model
provenance, manual titles and audio references were preserved. The earlier batch
of regenerated notes keeps its original GPT-5.6 model attribution; switching the
configuration does not rewrite that history or regenerate the library.

## GPT-6.1 Sol migration (2026-10-01)

The default Codex model moved from `gpt-6-sol` to `gpt-6.1-sol`. After a synthetic
Codex inference and the provider health probe passed, nine profile versions
(44–52) were created with the same selections except the text-stage model; the
374 recording overrides on `qwen-nemotron` v1 moved to its v3 counterpart, and
`qwen-nemotron` v4 is the system default. All 959 recordings resolve correction,
summary, mind map and Ask to `gpt-6.1-sol`. The earlier profile versions, the
`gpt-6-sol` catalog entry and all historical model attribution are unchanged.
Pre-migration backup: `data/backups/localplaud-pre-gpt61-20261001.db`.

## gpt-6.1-sol operating limits (2026-10-02)

With high reasoning effort, `gpt-6.1-sol` calls take several times longer than
`gpt-6-sol`. A correction request near 19k CJK characters and evidence-note
extraction batches near 14k transcript characters exceeded the 900-second
per-call limit, so no note completed. The production Codex connection therefore
uses `timeout_seconds = 1800`, `polish_chunk_chars = 8000`, and
`summary_chunk_chars = 32000` (about 3.5k transcript characters per extraction
batch after prompt and context overhead). Correction requests that still time out
are split and retried. Changing timeout or quota settings does not invalidate
completed stages; changing a chunk budget re-runs only the stage that uses it.

## Central AI gateway (2026-10-02)

Text stages now reach the model through the sky-mini AI gateway
(`~/Projects/ai-gateway`, CLIProxyAPI on `127.0.0.1:8317`) instead of spawning
`codex exec`. Profiles select the semantic alias, never a concrete model:

| Stage | Alias | Why |
|---|---|---|
| `correct` | `sky-quality` | the corrected transcript is the canonical text a person reads and edits |
| `summarize` | `sky-quality` | notes are read directly |
| `mind_map` (and outline) | `sky-quality` | read directly |
| `ask` | `sky-quality` | answers are read directly and must stay grounded |

`sky-quality` currently resolves to `gpt-6.1-sol`, the model these stages already
used, so the migration changes the route, not the model. `sky-fast` is catalogued
but not selected.

- **Connection** `llm:ai-gateway` (`provider_type = "ai-gateway"`, cloud, egress).
  The client key is `secret_ref = env:LOCALPLAUD_AI_GATEWAY_KEY` in the gitignored
  `.env`. Connection settings carry over the GPT-6.1 limits: high reasoning,
  Responses streaming, `timeout_seconds = 1800` per call without SDK retries,
  `polish_chunk_chars = 8000`, `summary_chunk_chars = 32000`.
- **Subscription reserve.** The gateway's only upstream is the same ChatGPT Pro
  account as `~/.codex`. Before every call the adapter reads that login's window
  through `codex app-server` (`account/rateLimits/read`) and refuses below 5% + 2%
  headroom, exactly like `codex-local`. `quota_account_id` pins the login to the
  gateway's account; a different login fails closed. A reported secondary window
  is honoured when it is tighter.
- **Provenance.** Stage attempts store the requested alias as `model` and the
  answering model under `usage.resolved_models` (also `stage_runs.detail`); Ask
  messages store it in `usage.ask.resolved_models`.
- **Reuse.** Each resolved profile records `alias_resolution` (policy revision and
  the alias's target, read from `model-policy.json`). Artifact reuse compares the
  target: remapping `sky-quality` invalidates reuse; a revision bump that leaves it
  alone does not; an unreadable policy never vouches for reuse.
- Embeddings, ASR, alignment and diarization are unchanged.

Production migration (2026-10-02, after the in-flight recovery run finished and
an online backup to `data/backups/localplaud-pre-ai-gateway-20261002.db`): nine
profile versions (53–61) copy versions 44–52 with only the four text stages moved
to `llm:ai-gateway`/`sky-quality`; `qwen-nemotron` v6 is the system default and the
374 recording overrides moved from `qwen-nemotron` v3 to v5. All 961 recordings
resolve every text stage to the gateway. Older profiles, the `codex-local`
connection and catalog, and all existing artifact attribution are unchanged. A
leaked dispatch reservation of a dead recovery process was closed through
`recover_provider_dispatch_reservations` first; it had blocked profile changes.

Verification: derived regeneration of one 2.8-minute recording whose text stages
had failed on the old Codex quota completed correction, notes and mind map through
the gateway. Each attempt records `provider = ai-gateway`, `model = sky-quality`
and `usage.resolved_models = {"sky-quality": ["gpt-6.1-sol"]}`; the new notes'
snapshots carry `alias_resolution = {"revision": "2026-10-02", "model":
"gpt-6.1-sol"}`. Model health for both aliases reported the shared window (37%
remaining, 5% protected); the same connection with a 50% reserve refused before
any request.
