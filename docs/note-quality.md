# Recording note quality

Plaud's visible template descriptions are useful format references. They are not
its hidden model prompts, and copying them does not reproduce its summaries.
Local notes therefore retain both the exact selected template snapshot and a
separate, versioned local execution policy (`recording-notes/v1`).

The execution policy asks for concrete context, reasons, alternatives, numbers,
decisions, unresolved questions and supported next steps. Suggestions must remain
distinct from decisions. Dates, roles and action owners must not be invented.
Chinese recordings use Traditional Chinese with Taiwan wording. User-authored and
specialist templates retain their selected format.

For the captured Autopilot template, short recordings use a direct call. Longer
recordings generate detailed topic sections for every transcript chunk. Those
sections are retained in the final note; hierarchical reduction is used only to
produce the overview, title and tags. This avoids requiring every detail to survive
repeated compression into a single short final answer. It can leave neighboring
sections on related topics: it is not a guarantee of globally deduplicated prose.
Other templates retain their existing full-coverage hierarchy.

The direct output allowance is 3,000 tokens, section outputs 2,400 each and the
overview 800. These are provider request budgets, not a target length or a claim
that every adapter enforces them. The execution snapshot records those budgets.
Invalid or incomplete structured note output fails before replacing an existing
note. Successful regeneration archives the previous note through normal history.
Remote requests carry the note policy version so a previous-policy cached result
cannot satisfy a new request; unsupported worker versions fail explicitly.

Ollama requests explicitly set `num_ctx` (8,192 by default), disable input
truncation and reject output ending at the token limit. Its summary chunks are
capped at 3,000 characters by default, reserving space for instructions and output.
Set `[llm.ollama] context_tokens` and `summary_chunk_chars` together on the actual
execution host. Larger windows consume more memory. Context overflows fail clearly;
no truncated note should be presented as a complete result.

## Evaluation and limits

Compare the same recording's local transcript, local note and an explicitly
requested read-only reference. Check distinct topic coverage, late decisions,
numbers and units, proposal-versus-decision status, unsupported claims, action
ownership and repetition. A longer answer is not evidence of improved accuracy.
Keep recordings, reference notes and evaluation drafts outside the repository.

An initial private comparison found both compression loss and model errors:
existing transcript facts were omitted or their meaning reversed. A more detailed
draft on the same small model still invented actions. That draft was not accepted
as a replacement. Prompt changes alone do not establish Plaud-equivalent quality;
review representative real outputs with the selected provider before bulk repairs.
No model or recording-profile change is implicit in this implementation.

The policy was deployed to the controller and GPU worker on 2026-09-23. Following
explicit user authorization, a reviewed Codex pilot was used to regenerate the
20 selected recordings' primary Autopilot notes with the configured
`codex-local` / `gpt-5.6-sol` connection. Recording-scoped profiles select Codex
for summarization, retain other stage selections and disable summary fallback;
the library's default local-model profile was not changed by that repair. A later
explicit request migrated active text settings to [GPT-6](gpt6-settings.md),
without relabelling these existing notes. Generation used local
canonical transcripts, never the Plaud reference notes.

Verification checked completed stage provenance, policy snapshots, unchanged
transcript lineage and content, retained manual titles/audio paths, and exact
preservation of each displaced note in revision history. Representative note
pages rendered the new content. Review of a long recording checked concrete
amounts, timing and proposed versus agreed actions against the transcript.
This is a bounded repair, not a claim of Plaud-equivalent accuracy across the
library. Inadequate transcripts produce explicit limitations; they still require
acoustic recovery. Other note templates were retained, and existing mind maps
were marked stale rather than silently treated as current.

Existing completed notes are not silently invalidated by a policy upgrade. Resume
can reuse them; explicit regeneration selects the new policy and preserves the
displaced output in history. Review and select affected recordings before a bulk
regeneration, particularly where transcript quality or user edits differ.

Plaud-derived content remains comparison/debug input only. Normal generation uses
the corrected local transcript and the explicitly selected stage provider. Poor
ASR must be repaired from audio; copying a cloud note cannot repair local evidence.
See [speech quality](speech-quality.md) for safe acoustic recovery.
