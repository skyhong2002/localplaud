# Qwen + Nemotron GPU speech profile

The opt-in production profile uses Qwen3-ASR 1.7B, Qwen3 ForcedAligner 0.6B,
and NVIDIA Nemotron-3-Diarization. It was selected after the September 24,
2026 evaluation of Taiwan Mandarin and mixed Mandarin/English recordings.
Plaud transcripts were evaluation references only; production needs raw audio.

Qwen always runs CPU Silero ONNX VAD before transcription. Speech regions keep
short pauses and boundary padding, omit long silences, and are split into at
most 120-second inputs (configurable, maximum 240). ASR and forced alignment
run sequentially in one disposable GPU process; Nemotron runs in another.
Model revisions, VAD settings, speech/skipped seconds, and original-timeline
provenance are saved on the transcription stage. Point word timestamps are
retained. Inputs that hit the generation limit are bisected and retried without
repeating successful regions; the split history is recorded. Private ASR chunk
checkpoints in `data/speech-checkpoints` are keyed by audio bytes and configuration,
so a worker restart can reuse completed decoding. These files contain transcript
text and must remain private alongside the other local recording artifacts. When an isolated Qwen or Nemotron process fails, the stage error stays
sanitized to the exception name, and the tail of that process's private log is kept
owner-only under `data/speech-runtime-failures` on the machine that ran it (newest 20),
so the real cause can be read there without exposing transcript text in the Web App. Irreducible token
limits or incomplete alignment fail the stage rather than storing partial text.
Original audio is unchanged; this does not create a shortened playback file.

Nemotron processes the entire recording with a continuous speaker cache,
so independently transcribed chunks do not reset speaker identities. It supports
at most eight speakers. Returned turns are clipped to the recording boundary.
The align stage validates the forced alignment already computed with Qwen;
it does not run WhisperX over the new transcript. Qwen ASR supports more languages
than its forced aligner. Unsupported detected languages retain their complete
text and segment timestamps; the align stage explicitly becomes degraded,
while diarization and notes can continue. No language is silently substituted.

## Deployment

Build the normal CUDA image first, then `Dockerfile.speech`. The overlay retains
the existing CUDA runtime and installs the tested NeMo dependencies into
`/opt/speech`, with the base runtime on its Python path. The base must provide
Torch 2.8.0 CUDA 12.8, Transformers 5.15.1, and faster-whisper 1.2.1. Dependencies
are recorded in `requirements/speech-cuda.txt`; NeMo is pinned to a source
archive and hash because older published packages cannot load this checkpoint.
Keep the base image's immutable image ID with the deployment record.

Apply `docker-compose.speech.yml` after the existing compose files. It selects
the isolated interpreter for both speech stages. Download these pinned models
into the normal persistent Hugging Face cache before activation:

| Model | Revision |
| --- | --- |
| Qwen/Qwen3-ASR-1.7B-hf | bcd2b5b7f32b480ab5790554cfa8347f246a14f3 |
| Qwen/Qwen3-ForcedAligner-0.6B-hf | c07281df297b9905d24a508279258cccf987a064 |
| nvidia/Nemotron-3-Diarization | a435e9867d79e789e90053f9b6d6834053af564a |

Pause automatic processing, let active jobs finish, and stop the controller
scheduler during the profile transaction. Back up the controller
DB and configuration. Deploy the same source on controller and worker, initialize
the database to apply the additive `process_overlong` migration, then verify a
real speech clip and an all-silent clip in the new image. No speech fallback is
configured by this rollout.

On the controller, run:

```sh
python scripts/maintenance/activate_qwen_nemotron.py --worker-key WORKER_KEY
python scripts/maintenance/activate_qwen_nemotron.py --worker-key WORKER_KEY --apply
```

The first invocation is read-only. Activation verifies worker capabilities,
creates an immutable default profile, preserves the existing non-speech stage
selections, and queues non-trash unfinished recordings. Completed recordings
and existing transcript revisions are preserved. Missing audio enters the
read-only Plaud download queue. Existing transcripts retain their prior alignment
selection; incomplete diarization can resume with Nemotron. Explicitly queued
recordings may exceed the ordinary duration cap, without removing the cap for
future automatic discovery. Resume automatic processing after activation.

Failures remain in the durable stage ledger and normal bounded retries apply.
A recording with an unreadable or missing raw file cannot be declared complete.
To roll back, select the previous immutable profile and the original GPU image;
do not restore a stale DB backup over newer user edits.

## Long recordings and memory

Nemotron diarization of a long recording needs the log-mel features of the whole
recording on the host, because the speaker cache must not reset between chunks.
Computed in one call that is about 2 GB of host memory per hour of audio: five hour
recordings reached about 10 GB resident plus 2.2 GB shared on the 16 GB WSL worker and
were killed by the kernel's out-of-memory killer (exit -9) whenever other work held a
few GB. Features are now computed in 10 minute slabs, each with a few real neighbouring
frames of context, and are bit-identical to one whole-signal call (maximum difference 0
on the real preprocessor over 25 minutes of audio at three slab sizes; the test
suite checks the same equality with a stand-in that reproduces the framing and padding).
Peak host memory for a five hour recording fell from above 12 GB (the old path was killed
in a replay on an otherwise quiet worker) to 5.3 GB, and diarization took 50 seconds.

A speech process that is killed or runs out of memory is reported as resource
exhaustion (`worker_resource_exhausted`, retryable). The controller then pauses
(`pipeline.speech_resource_retry_seconds`, default 60) and attempts the diarize stage again
up to `pipeline.speech_resource_retries` (default 2) times before it degrades the stage.
Each attempt is a recorded stage attempt, and notes are not generated on a transcript that
is about to gain its speakers. Deterministic failures and timeouts are never retried this
way.

