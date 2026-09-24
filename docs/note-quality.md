# Recording note quality

The default `evidence-notes/v2` policy generates notes from the canonical local
transcript. Plaud descriptions inform the selected template's format, but neither
Plaud notes nor Plaud transcripts are normal generation inputs. The exact template
snapshot is retained separately from our execution policy.

## Generation and recovery

1. Assess timestamps, speaker labels, sparse speech, gaps and repeated phrases.
   Invalid timestamps and long fixed English filler loops block new derived
   artifacts and expose a degraded state. Sparse speech or missing speakers alone
   produce warnings: these checks do not classify music or silence as failed ASR.
2. Split the complete transcript into timestamped source parts, retaining every
   nonempty segment and the tail of oversized utterances. Neighbor context is
   marked separately from each chunk's target evidence.
3. Extract atomic facts with topic, type, status, nullable owner/deadline and exact
   source quotations. Every source part must be cited or carry an explicit reason
   for exclusion; a model reviewer checks those exclusions too. A separate call
   checks the extraction against the
   original chunk for omissions, invented commitments and reversed conditions.
4. Plan descriptive topic sections. Each extracted fact must be assigned exactly
   once. Only identical claims with matching status/ownership collapse; conflicting
   or revised claims remain distinct. If the ledger exceeds the request budget,
   bounded groups are planned separately, so global deduplication is not promised.
5. Draft sections in bounded groups against both facts and their original source passages. Keep
   reasons, alternatives, event/number pairs and uncertainty. Technical discussion,
   teaching, calls and informal conversation need different structures; explicit
   specialist templates retain priority. Recorded actions are distinct from ideas.
6. Independently verify each draft against its source passages. Repair extraction
   or prose at most twice; continuing material verification failures leave the stage failed
   and existing notes intact. Noncritical wording observations are retained separately
   in provenance; they are not a claim of perfect accuracy. Timestamp links open
   the recording at the evidence.
7. Save accepted notes through normal revision history and invalidate a dependent
   mind map when its source changes. Canonical transcript, speaker and note
   fingerprints reject a save if the user edited the inputs during generation.
   When evidence-note generation fails, preserve the old dependent mind map in a
   degraded state; indexing the usable canonical transcript can still complete.
   Review failures include the remaining material issues for targeted recovery.

The ledger stores source IDs, timestamps, quotations, source digest and the
provider/model. Coverage and phase usage describe executed checks; they are not
measurements of semantic accuracy. A model reviewer is not independent human or
audio verification and can share the generator's mistakes.

## Configuration

```toml
[pipeline]
note_quality = "evidence"       # default; "legacy" explicitly selects the v1 algorithm
note_evidence_chunk_chars = 120000 # complete request budget, capped by provider capability
note_repair_attempts = 2        # 0..2; never an unbounded repair loop
```

The existing stage-scoped provider profile still selects the model, egress policy,
fallbacks and cost ceiling. This feature does not authorize a new provider or
silently downgrade a model. The production GPT-6 configuration is documented in
[gpt6-settings.md](gpt6-settings.md). ASR/diarization/embedding models retain their
own capability requirements.

Successful validated subcalls are checkpointed under `data/note-checkpoints/`
(or beside the configured audio download directory), using private directory/file
permissions. Keys include the source/context, provider configuration digest,
prompt, schema and policy version. Retry reuses matching subcalls; a changed
source/model/prompt cannot replay an incompatible answer. Checkpoints contain
private recording content: back them up as private data, never commit them.
A failed stage retains its reservation under the existing cost accounting policy.
Rejected candidates keep their last review feedback in a separate checkpoint.
A later stage retry continues that repair instead of replaying the same rejected
answer. Already reviewed chunks and drafts remain reusable. Each invocation still
has the configured repair limit and the normal stage retry/cost policy applies;
source, model or review-prompt changes invalidate the continuation.
Fact repairs specify indexed replacements, additions, removals and exclusion
updates. Unmentioned facts stay byte-for-byte equivalent instead of being
regenerated; the merged ledger must still pass quotation/coverage validation and
source review. Repair checkpoints are compatible with previously reviewed facts
because the source-review criteria are unchanged.

The request budget includes serialized source metadata and schemas, not just
transcript characters. Small local-model contexts may require substantially more
calls. Oversized evidence fails explicitly rather than truncating. Codex CLI does
not enforce the adapter's `max_tokens` argument; token settings are request
allowances, not a guarantee of output length. Evidence generation uses more model
calls than the legacy summarizer and remains subject to quota reserve checks.

Controller and remote worker must agree on `evidence-notes/v2`. Remote cache keys
include the policy and quality options. A manual resume regenerates a mismatched
policy/mode while preserving history; deploying alone does not enqueue the entire
completed library. Bad ASR requires acoustic recovery, never replacement with a
Plaud reference transcript. See [speech-quality.md](speech-quality.md).

## Evaluation and rollout

Use user-owned recordings in a private manifest. Preserve old notes, canonical
transcript revisions/hashes, model/profile and recording dates. Split development
and validation cases by discussion group. Evaluate note generation on a fixed
local transcript separately from the full raw-audio pipeline.

Check early/middle/late topics, conditions, negation, event-number pairing,
proposal-versus-decision status, invented owners/deadlines, meaningful detail and
repetition. Synthetic contract tests exercise failures and recovery; they do not
prove model accuracy. A longer note is not by itself an improvement.

A private monthly audit motivated this policy: it found template leakage,
compression loss, misattributed decisions and unusable upstream transcripts.
Reference notes are comparison/debug material only. Targets such as 95% annotated
important-fact coverage and zero critical reversals require a separately recorded
human evaluation; they are not established by this implementation. Qualify real
candidates before bulk regeneration and record failures instead of claiming that
all recordings reached completion.
