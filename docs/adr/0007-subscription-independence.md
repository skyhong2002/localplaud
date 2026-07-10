# ADR 0007: Plaud subscription independence and artifact provenance

Status: Accepted; core provenance enforcement implemented 2026-07-10

## Context

The first implementation could mirror Plaud-produced transcripts and summaries and
reuse them in the local pipeline. That accelerates migration and comparison, but it
does not replace Plaud Intelligence: those artifacts may consume Plaud transcription
minutes and disappear for new recordings when the user stops using paid generation.

The product goal is to retain the Plaud recorder and raw-audio upload convenience
while independently providing the processing and Web App workflow.

## Decision

- The primary pipeline consumes Plaud recording metadata and raw audio only.
- Plaud transcript, summary, outline, embedding, and AI-task artifacts are never an
  implicit fallback or completion signal.
- Cloud-artifact import may remain as an explicit migration/debug feature. Imported
  artifacts retain `source=plaud`, never overwrite a canonical local artifact, and
  remain visibly distinguishable in the Web App.
- Existing databases need a migration path that preserves imported content while
  queuing raw audio for independent processing.
- Completion is derived from durable local stage results. Each artifact records its
  source, provider/model, configuration/prompt version, timestamps, and revision
  lineage.
- Subscription-independence acceptance tests use recordings for which Plaud has not
  generated a transcript or summary.

## Consequences

- `pipeline.artifact_mode = "independent"` is the default and provenance-aware
  transcript selection is enforced. `prefer_cloud_artifacts` has an effect only in
  explicit migration mode.
- Multiple transcript rows preserve both Plaud imports and the canonical local ASR
  result. The one-time independent migration requeues cloud-only files, relabels
  ambiguous local summaries as legacy, and clears their non-provenanced index chunks.
- Initial backlog processing takes longer because all recordings need local work.
- Imported Plaud output remains valuable for benchmark comparisons without becoming
  a production dependency.
- The Web App must expose source and degraded/partial states honestly.
