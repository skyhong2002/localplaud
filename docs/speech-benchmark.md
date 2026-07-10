# Speech quality acceptance benchmark

Use `localplaud benchmark-speech` before changing the production ASR, alignment,
VAD, or diarization default. The harness compares already-produced transcript
artifacts, so it does not invoke a provider, upload audio, or include recordings in
the repository. Use only recordings you own or are licensed to evaluate and for
which the speakers consented to this use.

## Dataset layout

Keep the private dataset outside the repository. Paths must be relative to the
manifest, cannot escape its directory, and cannot be URLs. Each reference and
prediction JSON uses this shape:

```json
{
  "segments": [
    {"text": "大家好", "start": 0.4, "end": 1.2, "speaker": "speaker-a"}
  ],
  "audio_duration_seconds": 60.0,
  "runtime_seconds": 12.4,
  "peak_memory_mb": 1832.0
}
```

`runtime_seconds` and `peak_memory_mb` belong on the prediction and should be
captured by the ASR runner in the same process environment used for the benchmark.
The reference may omit them. Speaker labels need only be consistent within a file;
the evaluator finds the best hypothesis-to-reference label mapping.

Example `manifest.json`:

```json
{
  "schema_version": 1,
  "dataset_name": "private-tw-mandarin-codeswitch-v1",
  "cases": [
    {
      "id": "meeting-01",
      "language": "zh-TW+en",
      "reference": "references/meeting-01.json",
      "prediction": "runs/mlx-turbo/meeting-01.json",
      "consent": true,
      "ownership": "user-owned"
    }
  ],
  "thresholds": {
    "max_cer": 0.15,
    "max_der": 0.20,
    "max_timestamp_mae_seconds": 0.50,
    "max_hallucination_rate": 0.03,
    "max_realtime_factor": 0.50,
    "max_peak_memory_mb": 6000
  }
}
```

Run it with:

```console
localplaud benchmark-speech /private/path/manifest.json \
  --output /private/path/report.json
```

Exit status is 0 when every case passes every configured threshold, 1 for an
acceptance failure, and 2 for an invalid manifest/artifact. A configured metric
that cannot be measured fails closed. The JSON report is safe to compare across
runs because it contains case IDs and numeric metrics only, never transcript text
or artifact paths. Case IDs and dataset names should therefore also be anonymous.

## Metrics and limitations

- WER tokenizes Latin words and individual Han characters; CER removes whitespace
  and punctuation. CER is normally the primary Mandarin accuracy measure.
- Hallucination rate is the insertion component of the same minimum-edit alignment.
  Include silent/no-speech reference cases to exercise hallucination behavior.
- Timestamp error is mean absolute start/end boundary error for positionally paired
  non-empty segments. Keep reference segmentation stable when comparing runs.
- DER is a collar-free, duration-weighted single-speaker approximation with optimal
  speaker-label mapping. The compact schema cannot represent overlapping speakers;
  use a dedicated tool such as pyannote.metrics for overlap-aware research results.
- Realtime factor is runtime divided by audio duration. Peak memory is reported in
  MB as captured by the runner.

Do not commit manifests, reference transcripts, predictions, numeric reports with
sensitive case identifiers, or recordings. The harness itself ships no dataset.
