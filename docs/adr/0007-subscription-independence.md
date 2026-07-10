# ADR 0007: Plaud subscription independence and artifact provenance

Status: Accepted

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

- Setting `prefer_cloud_artifacts=false` is necessary but insufficient while old
  cloud transcript rows can still be reused. The pipeline and schema need explicit
  provenance-aware selection.
- Initial backlog processing takes longer because all recordings need local work.
- Imported Plaud output remains valuable for benchmark comparisons without becoming
  a production dependency.
- The Web App must expose source and degraded/partial states honestly.
