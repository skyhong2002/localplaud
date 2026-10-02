# Cross-recording speaker identity

The optional voice service watches the library independently of ASR and notes.
It reads the canonical local speaker timeline, extracts up to six clean speech
windows per voice, and matches NVIDIA TitaNet-Large voiceprints. It runs the pinned
model on the explicitly configured SSH worker's **CPU**, independently of the GPU
speech queue. Clips are sent over authenticated SSH; no external inference provider
or Plaud Generate call is used. The ASR/diarization profile stays unchanged.

The whole library is scanned on each cycle. New recordings and changed speaker
assignments create new fingerprinted samples. Completed samples are reused; failed
extractions retry after an hour. Active recordings and trash are excluded. If raw
audio was evicted, `download_missing_audio` explicitly permits a temporary read-only
Plaud raw-audio download. Only service-owned temporary audio is deleted.

## Enrollment and matching

Local manual speaker names enroll automatically. `import_plaud_names` is an
**opt-in migration input** for existing Plaud name labels: it cannot prove which
labels were manually entered, and it preserves `plaud-reference` provenance.
Anonymous and combined-person names are excluded. Inferred local names never
become enrollment references. Different spellings/case are not silently merged.

A match requires at least two query windows, agreement across at least two thirds
of them, references from at least two OTHER recordings, a cosine threshold and a
margin over other names. Scores are similarities, not accuracy percentages.
Insufficient, ambiguous and unknown voices keep their existing labels. Reference
windows with poor internal similarity are excluded. Human names, clears, and
subsequent edits are protected; inferred names can be corrected in the existing
speaker rename control. Such corrections become manual references on the next scan.

`voice_samples` stores model/revision, source, timestamp windows, fingerprint,
vectors, status and error. `voice_assignments` records candidate/assignment scores,
reference IDs, thresholds, and override state. `voice_identity_events` preserves
application/undo history. Original transcripts and audio are never changed.
Name application uses the existing transcript mutation lock and invalidates notes,
mind maps and search chunks through the same durable reindex queue as a manual
speaker rename. Existing generated notes are retained as stale until regenerated. Restricted
new-recording mode drains this reindex queue only for recordings transcribed within
`pipeline.untranscribed_only_retry_hours`; it does not resume historical backfills
or spend LLM quota regenerating notes automatically after a name edit.

## Operation

Example private `data/voice-identity/profile.json` (set the worker explicitly):

```json
{
  "enabled": true,
  "ssh_host": "your-authorized-worker",
  "container": "localplaud-localplaud-gpu-1",
  "python": "/opt/speech/bin/python",
  "import_plaud_names": false,
  "download_missing_audio": false,
  "apply_names": false,
  "threshold": 0.75,
  "margin": 0.12,
  "poll_seconds": 300
}
```

Run `.venv/bin/python -m localplaud.voice_service --profile PATH --watch` under a
service supervisor, using the deployment's normal config and PATH. The profile is
reread each cycle; `enabled: false` pauses future cycles. A process lock prevents
duplicate watchers. Start with `apply_names: false` and optionally `--limit 25`
for a bounded enrollment smoke test, inspect `receipt.json`, then enable application.
With `--watch --limit 25`, each cycle extracts up to 25 additional recordings and
applies matches before continuing in the next cycle; cached samples are skipped,
so this eventually covers the whole library and continues with newly arriving files.
The receipt contains aggregate counts and a leave-recording-out comparison with
imported labels (a proxy, **not human-verified accuracy**). The full scan is resumable
at speaker sample granularity. Private `runtime.log` contains runtime diagnostics.

The worker needs the normal speech environment plus these source modules; no GPU
service restart is needed. Model:
`nvidia/speakerverification_en_titanet_large`, revision
`0dc382f40121a5fbd34db10a2bb04d826c2be6a8`, CC-BY-4.0.
[Model card and attribution](https://huggingface.co/nvidia/speakerverification_en_titanet_large).

An operator can call `voice_identity.undo_assignment(session, speaker_id)` to undo
an unchanged automated assignment. This preserves later human edits, records the
undo, suppresses reapplication, and queues reindexing. Voiceprints are private
biometric-derived data kept in the local database; include them in the same backup
and access controls as recordings. No voiceprints are committed to the repository.

## One-time confirmed enrollment

A private profile may contain `plaud_enrollment`, version 1, with an immutable
`id`, `created_at`, `confirmed_manual: true`, `min_recordings`, `names` (exact name
→ distinct recording count at selection time), and `samples` (sample fingerprint
ID → approved name). This freezes a user-approved migration enrollment. Only the
selected existing Plaud sample IDs may serve as references or enter extraction;
new Plaud labels, new recordings of a selected name, and changed sample timelines
do not silently expand the snapshot. Existing embeddings are reused. Malformed
snapshots fail closed. Omission retains the original broad opt-in import behavior;
an explicit empty snapshot permits no Plaud references.

Local manual names continue to enroll independently. Automated assignments never
enroll. Reference records carry `user-confirmed-manual` label provenance while
retaining `plaud-reference` source. New assignment evidence and cycle receipts
record the confirmation ID. Keep the actual name list and snapshot in private
operator data, never source control. Existing assignments and historical samples
are retained; the snapshot controls subsequent matching references.

Explicit user-approved identity merges can be configured with the private profile's
`speaker_name_aliases` map (old spelling → canonical name). The service pools both
sets of reference embeddings under the canonical name and emits that name for new
matches. Source labels, sample IDs, original enrollment counts and manual-label
provenance remain intact. Multiple labels in one recording still count as one
independent recording. Alias chains and cycles are rejected; case folding never
implicitly merges other names. Existing local display names, if present, require
an explicit rename through the normal guarded mutation and reindex flow.
