# Subscription-independence acceptance

`localplaud acceptance-check <recording-id>` audits one processed recording against
the product's raw-audio boundary. It is read-only: it does not generate Plaud AI
artifacts, rerun processing, or call an external provider.

The same report is available in the recording workspace under **Subscription
independence**, with per-check pass/fail evidence and a link to
`GET /api/files/{recording-id}/acceptance`. This keeps the product gate usable from
the Web App after setup; the CLI is an ops/automation convenience, not a requirement.

The gate requires all of the following evidence:

- the original or cached audio exists locally;
- the canonical transcript has `source=local`, timestamped segments, speaker
  assignments, and stable speaker rows;
- the durable align stage completed with `forced_alignment=true`; Whisper's own
  word timestamps remain useful degraded evidence but do not satisfy this check;
- at least one generated note and one mind map consume a local transcript;
- grounded retrieval chunks consume a local transcript, making single-file and
  library Ask retrieval-ready;
- durable stage rows have immutable resolved-profile snapshots and no failed stage;
- TXT, SRT, VTT, DOCX, and PDF render successfully from the local canonical transcript;
  generated/Saved notes render as Markdown, TXT, DOCX, and PDF.

Run the human-readable report:

```bash
localplaud acceptance-check RECORDING_ID
```

For automation, use `--json`. The command exits non-zero when any check fails:

```bash
localplaud acceptance-check RECORDING_ID --json
```

The automated acceptance test drives the daemon's discovery and processing jobs:
the first sync catalogs historical files without downloading them, and the next
sync discovers and downloads a new recording with no Plaud transcript or summary.
Network-free test providers execute ASR, forced
alignment, notes, mind map, embeddings, and grounded Ask; the test then runs the
same audit and verifies playable Ask citations plus TXT/SRT/VTT/DOCX/PDF.
Hardware/model quality review remains an engineering concern rather than a
user-facing product feature.

## Automatic processing acceptance

After OAuth and provider setup, enable automatic processing in Workspace settings
and run the managed `localplaud run` service. A newly discovered upload must pass
the raw-audio gate without a Plaud Generate action or local per-stage commands.
Discovery and processing each allow one active scheduled job; processing keeps the
existing per-file claims, profile policies, retry backoff, and revision safeguards.
Losing daemon ownership pauses new invocations of both jobs; in-flight publications
retain their existing durable claim fences. Disabling cloud polling still permits
enabled automatic processing to resume already downloaded recordings.
Metadata AutoFlow rules run before new downloads become worker-eligible. A rule
blocked by an active processing claim remains eligible for later evaluation, rather
than being recorded as a permanent failure for that rule version. Cloud listing
pages are fetched outside the database write transaction so network delays do not
block worker progress.
The managed worker drains persisted note-index jobs without repeating a full
library write transaction each tick. Startup, artifact edits, and profile changes
still reconcile the affected documents; standalone `work` retains full discovery.

Regression tests hold processing open while discovery completes, reject overlapping
worker calls, exercise independent failures and subsequent recovery, and verify
ownership loss. Test doubles establish orchestration and artifact contracts; they
do not establish speech accuracy. A real-provider pilot must separately record its
initial absence of derived artifacts, completed stage/model provenance, current
transcript lineage, acceptance report, and a grounded Ask citation. Keep private
recording content and those pilot artifacts out of the repository. If the configured
audio-cache policy evicts a completed Plaud download, restore it through the normal
audio cache path before running the gate's local-audio check.

Automatic completion is not a guarantee that every word was recognized correctly.
Contextual corrections receive a separate evidence review; uncertain edits may be
rejected, and provider failures remain visible rather than silently selecting a
different provider. Existing recordings with exhausted retries require Resume;
this change does not silently reprocess the historical library.
