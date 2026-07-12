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

For a repeatable multi-recording hardware/profile gate, keep a private suite manifest
beside the private references and run:

```bash
localplaud benchmark-suite /private/path/suite.json \
  --output /private/path/apple-mlx-report.json
```

```json
{
  "schema": "localplaud-benchmark-suite/v1",
  "name": "Taiwan Mandarin and code-switch acceptance",
  "target": "apple-mlx",
  "thresholds": {
    "cer": 0.12,
    "der": 0.20,
    "speech_character_insertion_rate": 0.04,
    "real_time_factor": 1.0
  },
  "cases": [
    {"id": "meeting-01", "file_id": "LOCAL_RECORDING_ID", "reference": "meeting-01.json"},
    {"id": "code-switch-01", "file_id": "LOCAL_RECORDING_ID_2", "reference": "code-switch-01.json"}
  ]
}
```

Relative reference paths resolve beside the manifest. A broken case is reported with
a generic error and does not prevent the remaining cases from running. Aggregates are
weighted by reference characters/words, speaker-time, paired timestamp segments, or
audio duration as appropriate; peak memory is the maximum observed value. The suite
fails when any case fails or any configured maximum is exceeded. Supported gates are
`cer`, `wer`, `der`, `speech_character_insertion_rate`,
`speech_word_insertion_rate`, `non_speech_character_rate`,
`boundary_mae_seconds`, `real_time_factor`, and `peak_memory_mb`.

The suite report contains the sanitized per-recording reports, aggregate coverage,
and gate decisions. It never includes reference filenames, paths, or transcript text.
Use the same manifest/reference revision on each target; only the local recording IDs
may need a private per-host manifest copy.

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

Single-recording output uses `localplaud-benchmark-report/v1`; suite output uses
`localplaud-benchmark-suite-report/v1`. Keep reports for each profile/model/version;
compare equivalent recordings and reference revisions. Do not change a production
default based on a single recording.
