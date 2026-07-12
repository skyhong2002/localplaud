# Recording quality benchmarks

Use a consented, user-owned reference transcript to compare execution profiles on
Apple Silicon, NVIDIA/CUDA, CPU, cloud, or remote-worker targets:

```bash
localplaud benchmark-recording RECORDING_ID \
  --reference /private/path/reference.json --json
```

The recording workspace exposes the same evaluator through **Run quality benchmark**.
The browser sends the selected reference as a bounded multipart request to
`POST /api/files/{recording-id}/benchmark` and renders CER/WER/DER, boundary MAE,
real-time factor, and peak-memory availability. The server reads at most 5 MB into
memory, closes the upload, and does not persist its filename or content.

The reference stays outside the repository and database. The versioned report does
not contain transcript text, recording title, or the reference path, so reports can
be aggregated without copying private content. Review them before sharing: the
recording ID and model/provider names may still be identifying in some deployments.

## Reference format

```json
{
  "schema": "localplaud-benchmark-reference/v1",
  "language": "zh-TW+en",
  "case": "code-switch",
  "coverage": "full_audio",
  "segments": [
    {"start": 0.0, "end": 2.4, "speaker": "REF_1", "text": "今天 review 進度。"},
    {"start": 2.4, "end": 4.8, "speaker": "REF_2", "text": "下一步開始測試。"}
  ]
}
```

Every segment needs non-empty text and start/end seconds. Speaker labels need only be
stable within this reference; they do not have to match localplaud labels.
Set `coverage` to `full_audio` only when every speech region in the complete recording
has been annotated. Omit it or use another label for partial references.

## Metrics

- **CER**: Unicode-NFKC, case-folded character edit distance with whitespace removed.
- **WER**: whitespace-token edit distance. CER is the primary Taiwan Mandarin metric;
  WER remains useful for English/code-switch spans. Both expose deterministic
  substitution, deletion, and insertion counts without retaining aligned tokens.
- **DER**: zero-collar, time-weighted miss + false alarm + speaker confusion divided
  by reference speaker time. Overlapping speakers are counted independently, so two
  simultaneous reference voices contribute two speaker-seconds per elapsed second.
  Hypothesis speakers are mapped one-to-one to reference speakers by maximum speech
  overlap (exact assignment for up to 12 labels, deterministic bounded fallback for
  unusually large label sets).
- **Boundary MAE**: mean absolute start/end error when reference and hypothesis have
  the same segment count; otherwise reported as unavailable instead of inventing an
  alignment.
- **Non-speech hallucination rate**: only available for `coverage: full_audio`.
  Hypothesis characters are weighted by the fraction of their segment duration that
  falls outside all annotated speech intervals. The report also counts segments that
  are mostly outside speech. This detects invented text during annotated silence; it
  does not claim to detect semantic hallucinations inside real speech.
- **Speech insertion rates**: character and whitespace-token insertions from the
  reference alignment, divided by reference units. These quantify extra ASR content
  during real speech and remain available for partial references. They are a
  reproducible hallucination signal, not a semantic truth judgment; substitutions
  can still change meaning without increasing this metric.
- **Real-time factor**: latest completed transcribe attempt latency divided by audio
  duration. Provider/model and raw latency are included.
- **Peak memory**: the Python worker process RSS high-water mark observed when the
  transcribe attempt completes. This is cross-platform process telemetry, not a claim
  about memory exclusively owned by the ASR model. Older attempts remain `null`.

The output schema is `localplaud-benchmark-report/v1`. Keep the raw reports for each
profile/model/version; compare equivalent recordings and reference revisions. Do not
change a production default based on a single recording.
