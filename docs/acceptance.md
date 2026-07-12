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
- at least one generated note and one mind map consume a local transcript;
- grounded retrieval chunks consume a local transcript, making single-file and
  library Ask retrieval-ready;
- durable stage rows have immutable resolved-profile snapshots and no failed stage;
- TXT, SRT, and VTT render successfully from the local canonical transcript.

Run the human-readable report:

```bash
localplaud acceptance-check RECORDING_ID
```

For automation, use `--json`. The command exits non-zero when any check fails:

```bash
localplaud acceptance-check RECORDING_ID --json
```

The automated acceptance test starts from a clean, user-owned raw-audio row with no
Plaud transcript or summary. Network-free test providers execute ASR, notes, mind
map, embeddings, and grounded Ask; the test then runs the same audit and verifies
playable Ask citations plus TXT/SRT/VTT. Separate real-hardware benchmarks remain
required for accuracy, diarization error, runtime, and memory claims.
