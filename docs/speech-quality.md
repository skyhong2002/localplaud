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
