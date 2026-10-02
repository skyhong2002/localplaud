# Speech detection and transcript recovery

Whisper can invent subtitle credits, names, or repeated phrases on silence or
background noise. Filtering a name is unsafe: it can also be genuine speech.
localplaud instead uses audio evidence.

## Production speech gate

Enable `[asr.vad] enabled = true` on **both the controller and GPU worker**.
Each host has its own configuration. The CUDA image includes the `vad` extra.

With shared Silero installed, MLX transcribes temporary speech regions and
FasterWhisper decodes speech clips on the original timeline. Both disable
previous-text conditioning and enable Whisper's silence/hallucination heuristic.
A successful detector result with no speech returns an empty transcript without
invoking Whisper. The pipeline skips text-derived models for empty input; it
cannot invent a title, note, or mind map from silence. Playback and export retain
the original audio.

Dependency failures are distinct from silence. MLX logs degraded whole-file
decoding if Silero is unavailable; FasterWhisper logs its fallback to bundled
native VAD. Install `localplaud[vad]` to avoid these degraded paths. VAD cannot
guarantee perfect recognition: quiet speech, music, and background voices remain
difficult cases.

For confirmed missed quiet speech, Qwen supports explicitly disabling VAD for
one transcription stage with profile options `{"vad": {"enabled": false}}`.
The remote worker validates and applies these options to that job only. Qwen then
decodes bounded, contiguous windows covering the entire recording, including its
tail; token-limit retries still bisect windows without dropping either half.
Artifact metadata records VAD as disabled and zero skipped seconds. This increases
work and can expose the decoder to silence/noise, so it is an explicit recovery
choice, not a global default or a guarantee that every utterance is recognized.
Merely cleaning or polishing an old transcript cannot recover missing speech.

Operators can also persist this choice in a new execution-profile version for
future Qwen recordings after checking their own audio. Existing profile versions
and per-recording selections remain traceable. This is a coverage/compute tradeoff,
not a change of ASR provider or a promise of verbatim accuracy; validate quiet
speech and hallucinations on representative recordings before selecting it.

Nemotron recordings longer than ten minutes compute full-recording acoustic
features on CPU before transferring features to CUDA. This avoids a large CUDA
STFT allocation while retaining one continuous speaker cache; it does not split
the recording into independently numbered speaker groups. Host memory must still
accommodate the full-recording feature calculation.

## Recovering old output

`python -m localplaud.silence_repair plan RECORDING_ID --output private.jsonl`
creates an operator-reviewed acoustic plan. `apply private.jsonl` applies that
single plan. Keep plans private: they contain recording identity and the previous
generated title. Planning checks existing audio, restoring Plaud's raw audio cache
if needed, without changing transcripts.

Cleanup uses a conservative speech threshold and an extra one-second margin.
Only valid timestamped segments with no overlap with detected speech are removed.
Uncertain timestamps are retained. There is no name or phrase blacklist.
Application checks audio checksum, transcript/revision/title identity, processing
and Ask leases, and preserves recordings with human edits. It appends a canonical
revision, retains raw ASR and history, clears the stale generated title,
invalidates generated notes/maps/search, and queues transcript reindexing.
Manual titles and user-authored notes remain intact.

When repeated decoder output has also replaced real conversation, removing silent
segments is insufficient. An operator can re-transcribe and diarize with the
recording's configured worker, then apply the reviewed result through
`apply_retranscription`. It uses the same snapshot and human-edit safeguards and
records provider/model/profile provenance in a new revision. Regenerate notes
through the normal derived-artifact pipeline.

Original output remains accessible through raw view and revision history. Empty
corrected text displays “No recognizable speech”; it never silently falls back
to the old hallucinated raw text.

## Headerless raw Opus

Some original `.opus` uploads have no Ogg container. After ordinary ffmpeg
conversion fails, the converter supports one verified layout only: at least 50
complete 80-byte packets, every packet with mono wideband 20 ms TOC `0xb8`. It
validates the entire file, cross-checks packet duration against recording metadata
when available, wraps temporary Ogg pages with checksums and 48 kHz granule
positions, and decodes with strict ffmpeg error handling. Other layouts remain
explicit conversion errors. Source bytes are never changed; a failed conversion
preserves an existing WAV and removes temporary outputs. This is container
recovery, not audio synthesis or a Plaud transcript import.

Recording playback and waveform requests use an identity-keyed derived WAV cache
for that validated headerless layout. Ordinary audio stays on its existing path;
original audio export still returns the unchanged source bytes.

## Contextual spelling correction

`transcript-polish/v2` explicitly resolves homophones and word boundaries when
pronunciation and the supplied conversation strongly support one interpretation.
Preserving the intended name does not require preserving an obvious ASR spelling
error. Ambiguous proper names remain unchanged; no global homophone replacement
list or Plaud-generated text is used. Each output records changed segment IDs and
retains raw ASR and earlier revisions. This is text correction; it does not rerun
acoustic alignment or claim that new spellings were verified against audio.

Automatic text correction can follow acoustic cleanup or reviewed re-transcription.
It starts from that canonical revision, never from the discarded raw segments, and
stores an `ai_polish_after_speech` revision. Resuming preserves this corrected
acoustic structure instead of restoring removed hallucinations. Human edits and
restored revisions remain protected. Explicit corrections likewise start from the
canonical revision and invalidate dependent notes and indexes. A new prompt does
not automatically enqueue the whole library for rewriting.

## Automatic correction and edit review

`transcript-polish/v5` runs before notes for both normal ingestion and notes-only
resume/regeneration. It proposes contextual edits, then makes a separate model
call to review individual edits against the original dialogue and nearby speaker
context. Latin words and numbers stay atomic during edit extraction. Accepted
nonoverlapping edits are applied to the original text only after every review
batch validates. Rejecting one uncertain edit in a long segment does not discard
unrelated supported corrections. Each edit and decision is recorded in the
correction stage. Review responses list approved IDs and brief rejection reasons;
approved entries store a generic approval marker, not a model-written rationale.
Every ID must still appear exactly once, including punctuation edits. Accepted v4
revisions remain reusable because v5 changes the output protocol, not the review
policy. Adjacent replacements without an unchanged boundary remain
one review unit; accepting an edit requires support for the complete span.
There is no manual approval step and no library-wide homophone replacement list.

Proposal requests are bounded by the provider's `polish_chunk_chars`. A request
that exceeds the per-call timeout is retried as two smaller requests covering the
same segments; a single segment that still times out fails the stage for durable
retry. Transport and quota failures are not split.

Malformed, incomplete, or unavailable review fails the correction stage before a
new revision or dependent notes are saved. Durable retry runs correction again
without rerunning ASR, including notes-only jobs without retained audio. Existing human edits and restored revisions are protected;
matching model/profile/prompt revisions are reused. Older machine corrections
upgrade when the recording is next processed, using the original ASR or a compatible
acoustic revision rather than an older model rewrite. Raw replacement invalidates
old automatic correction inputs; manual vocabulary revisions remain protected. Every proposal/review dispatch uses
the selected correction profile and its cost boundary, without a hidden provider
fallback. The usage ledger includes review calls.

A separate model review can still share the proposing model's mistakes; this is
not an audio-verified transcript or a guarantee of zero missed corrections.

## Offline speech benchmark harness

`localplaud benchmark-speech` evaluates user-owned recordings without uploading
any audio. It runs conversion, configured VAD/ASR, alignment and diarization locally,
without writing transcripts, stage runs or edits into the library. Model weights may
be downloaded by the selected local runtimes; audio is never sent to a cloud API
or remote worker. Evaluation rejects non-local speech selections before reading
recording inputs and does not execute profile or provider fallbacks. Failures remain
visible per recording and cause exit status 1, while other recordings continue.
This harness exists; **no real-recording quality benchmark has been run as part of
its implementation**, and production defaults have not changed.

Create a private JSON manifest, with paths relative to the manifest directory:

```json
{
  "version": 1,
  "recordings": [
    {
      "id": "meeting-a",
      "owned": true,
      "audio": "meeting-a.wav",
      "reference": {
        "text_file": "meeting-a.txt",
        "rttm": "meeting-a.rttm",
        "rttm_recording_id": "meeting-a",
        "words": [{"text": "你好", "start": 0.5, "end": 1.0}],
        "non_speech": [{"start": 10.0, "end": 12.0}]
      }
    }
  ]
}
```

Use full reference text for the complete recording, either `text` or `text_file`.
`words`, `segments` (same text/start/end shape), RTTM speaker turns and `non_speech`
are optional. All times are finite seconds. Inline `speaker_turns` accept
`{start, end, speaker}` objects. Multi-recording RTTM files require
`rttm_recording_id`. `owned: true` is required on every manifest recording.
Do not use a partial reference as if it covered the full recording.

Choose a durable profile by database ID, or pass an explicit speech JSON file:

```sh
localplaud benchmark-speech private/manifest.json --profile-id 3 \
  --output private/profile-3-report.json
localplaud benchmark-speech private/manifest.json \
  --speech-config private/speech.json --json --output private/report.json
```

Example `speech.json`:

```json
{
  "asr": {
    "provider": "faster-whisper",
    "language": "zh",
    "faster_whisper": {"model": "large-v3-turbo", "device": "cpu", "compute_type": "int8"}
  },
  "vad": {"enabled": true},
  "align": {"provider": "whisperx", "model": "wav2vec2-auto", "options": {"device": "cpu"}},
  "diarize": {"provider": "pyannote", "device": "cpu", "model": "pyannote/speaker-diarization-community-1"}
}
```

Unspecified speech values inherit local installation settings. Alignment runs for
benchmark execution; use `provider-word-timestamps` to validate the ASR's timings
instead of forced alignment. Select `diarize.provider = "none"` to evaluate an ASR
without an additional speaker stage; absent speaker predictions still score as
missed speech when RTTM references exist. The profile mode reads an existing profile
without bootstrapping or editing it; all three speech selections must be local.
Cloud/remote text stages in that profile are not executed. A full local benchmark
requires the optional ASR, VAD, alignment and diarization dependencies and ffmpeg.

The default output is a readable table; `--json` prints the full versioned report.
`--output` saves the same JSON in either mode. Reports contain recording IDs,
configuration/provenance, errors and reference-derived diagnostic positions; keep
manifests and reports private. Output cannot overwrite the manifest, audio, reference
text, RTTM or selected speech-config file. Original audio and references are read-only.

### Metric definitions and limits

- **CER** uses Unicode NFKC, case folding and alphanumeric characters; whitespace
  and punctuation are ignored. Traditional/Simplified distinctions remain scored,
  so use consistent Taiwan Mandarin references and inspect script differences.
  Rates are `(substitutions + deletions + insertions) / reference units` and may
  exceed 100%. Empty references with hypothesis text have an undefined (`null`)
  rate and retain insertion counts; both empty yields zero.
- **Mixed WER/MER** uses one token per Mandarin Han character and one per English
  word, with case-insensitive Unicode normalization, apostrophes within English
  words, and contiguous digit runs. Chinese/English boundaries work without spaces.
  WER uses this mixed token convention; MER means **mixed error rate** here and
  has the same denominator. Neither claims linguistically segmented Chinese-word
  WER or match-error-rate. Edit counts are exact Levenshtein counts. Aggregate
  CER/WER/MER sum errors and denominators rather than averaging recording rates.
- **DER** uses optimal global speaker-label assignment, continuous-time intervals,
  zero collar and scored overlap. Missed speech, false alarms and confusion are
  reported separately; aggregate DER is weighted by reference speaker-seconds.
  Predictions use word speakers when available (inheriting segment speakers),
  otherwise transcript segment speakers. This evaluates the exported attribution,
  rather than claiming access to the diarizer's unpublished raw timeline.
- **Hallucination indicators** flag consecutive n-gram loops (1–8 mixed tokens,
  at least three repetitions) and textual segments overlapping annotated non-speech
  regions. Overlapping annotations are merged. These are inspection cues, not
  acoustic proof: repetition can be legitimate and coarse segment timing can
  overlap silence. Missing non-speech references yield `null`, not a clean result.
- **Timestamp deviation** compares matched lexical units in monotonic matching
  blocks; insertions/deletions remain unmatched. JSON includes matched/reference
  coverage, mean and maximum absolute start/end deviation. Word references use
  predicted words; segment references use predicted segments. Multi-token segment
  boundaries apply to each token and are not inferred word times. Aggregate MAE
  weights matched boundaries. Missing timing references yield `null`.
- **RTF** is conversion plus VAD/ASR/alignment/diarization wall time divided by actual
  decoded audio duration. Model loading is included; spawn startup and scoring are
  excluded. Each recording runs in a fresh process to isolate memory measurements.
- **Peak memory** is process-tree CPU RSS sampled every 100 ms, with the largest
  process high-water RSS as a lower bound. RSS can double-count shared pages and
  sampling can miss brief peaks. GPU VRAM is excluded; it is not a GPU memory claim.
  Aggregate peak is the maximum observed recording peak, and aggregate RTF uses
  total elapsed time divided by total audio duration.

Pure metric tests use synthetic text, turns and timings, and harness tests use fake
execution. They download no models and do not establish real speech quality. Run a
private, reviewed cohort of Taiwan Mandarin and mixed Mandarin/English recordings
before changing production speech defaults or assigning catalog quality ratings.
