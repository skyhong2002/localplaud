# Speech benchmark runbook: Taiwan Mandarin, WSL CUDA and Mac↔WSL

This runbook turns the P0 speech validation debt in `TODO.md` into steps an
operator can follow on the production pair (Mac mini controller, WSL RTX 5060
worker). It covers three checks:

1. **Tooling smoke test** on the synthetic Taiwan Mandarin cohort (any host).
2. **WSL CUDA validation**: faster-whisper `large-v3-turbo` + WhisperX alignment
   + pyannote on the RTX 5060, on the same cohort and then on owned recordings.
3. **Mac↔WSL artifact contract** (product-workflow acceptance scenario 8): the
   same audio processed on both hosts must give comparable timestamped,
   diarized transcripts.

Nothing here reads or writes the production library. The benchmark converts
audio into a temporary directory, runs the speech stages in a child process,
and writes only the report and hypothesis files you name. Keep every manifest,
recording, report and hypothesis directory private and outside the repository.

Status on 2026-11-13: step 1 has been run on a shared CPU-only Linux host. Steps
2–4 need the production hosts and have **not** been run.

Step 1 smoke result (synthetic cohort, 183 s of TTS audio, faster-whisper
`large-v3-turbo` int8 on CPU with VAD, diarization off because the gated
pyannote model was not used): strict CER 25.6%, script-folded CER 7.8%, mixed
MER 32.2%, no repeated loops and no text over the inserted pauses. Word-time MAE
against the TTS boundaries was 0.28 s with WhisperX alignment and 0.14 s with
Whisper's own word timestamps; RTF 2.79 vs 0.52 (the WhisperX run includes the
first model download) and peak RSS 3.7 vs 2.3 GiB. DER is 100% by construction
without diarization, and the host-contract check correctly reports missing
speakers. These numbers only show the tooling works end to end: TTS speech and
TTS word boundaries are not a basis for choosing an aligner or a default.

## Scoring method

`localplaud benchmark-speech` reports, per recording and in aggregate:

| Metric | Meaning | Use |
| --- | --- | --- |
| CER | Character error rate, strict: Simplified vs Traditional counts as an error | Script choice of the raw ASR |
| CER (script-folded) | CER after folding Simplified to Traditional with OpenCC | Recognition accuracy; gate metric |
| Mixed WER/MER | Han characters plus English words as units | Code-switch accuracy |
| DER | Diarization error with optimal speaker mapping, zero collar | Speaker attribution |
| Time MAE | Mean absolute word/segment boundary error in seconds | Alignment quality |
| Indicators | Repeated n-gram loops; text over annotated non-speech | Hallucination cues |
| RTF, peak CPU RSS | Wall time ÷ audio time; sampled memory (no VRAM) | Speed/capacity |

Definitions are in [speech-quality.md](speech-quality.md#metric-definitions-and-limits).
The raw ASR stage keeps Whisper's Simplified output; the correction stage later
converts it to Taiwan script, so the script-folded CER is the fair accuracy
number for comparing ASR configurations.

`localplaud benchmark-speech-compare` turns two runs into a decision:

- `--baseline A.json --candidate B.json` compares two configurations on the same
  manifest. **Not worse** requires, on the recordings both runs completed:
  script-folded CER and MER up by at most 0.5 percentage points, DER up by at
  most 1 point, time MAE up by at most 0.05 s, no recording newly showing a
  hallucination indicator, no single recording's CER up by more than 3 points,
  and RTF at most 1.5× the baseline.
- `--hypotheses-a DIR --hypotheses-b DIR` compares the same configuration on two
  hosts, using directories written by `benchmark-speech --save-transcripts`.
  **Comparable** requires each host to time at least 95% of words and attach a
  speaker to at least 95% of words, ordered in-range segments, and between
  hosts a script-folded CER of at most 5%, a word-time MAE of at most 0.25 s and
  a speaker disagreement (DER of host B against host A) of at most 10%.

These thresholds are proposed review gates, not measured results; override any
of them with `--gates gates.json`. A comparison is labelled `evidence:
tooling-only` unless every compared recording is `source: owned`. Only owned
recordings can justify changing a production default (AGENTS.md).

## 1. Synthetic cohort (tooling smoke test)

Generate four short fictional dialogues (two- and three-speaker meetings, a long
low-noise pause, dense code-switching; about three minutes in total). The
`edge-tts` backend sends only the script text in
`scripts/benchmark/zh_tw_cohort/*.json` to Microsoft's TTS service; use
`--backend command` with a local TTS engine to stay offline.

```sh
uv run --with edge-tts==7.2.3 python scripts/benchmark/synthesize_speech_cohort.py \
  --out ~/lp-bench/cohort --backend edge-tts --allow-network-tts
```

The output holds `*.wav`, `manifest.json` (reference text, word times from TTS
word boundaries, speaker turns and non-speech regions) and `PROVENANCE.json`.
TTS speech is cleaner than real meetings: treat these numbers as a smoke test.

## 2. WSL CUDA validation

Run on the WSL host in a **separate checkout** of the branch you are testing,
never in the worker's live `src/` bind mount (the worker would pick the code up
without a restart). Use the production image so CUDA, cuDNN and model versions
match, with its own data directory and database.

1. Check that the worker is idle. In the controller Web App, Settings → Remote
   workers shows `queued_jobs` and `running_jobs`; both must be 0. Also confirm
   CUDA on the host: `/usr/lib/wsl/lib/nvidia-smi`.
2. Prepare the checkout and the private bench directory:

   ```sh
   git -C ~/localplaud-bench fetch && git -C ~/localplaud-bench checkout <branch>
   mkdir -p ~/lp-bench && cp -r <cohort-or-owned-manifest-dir> ~/lp-bench/
   cat > ~/lp-bench/speech-cuda.json <<'JSON'
   {"asr": {"provider": "faster-whisper", "language": "zh",
            "faster_whisper": {"model": "large-v3-turbo", "device": "cuda", "compute_type": "float16"}},
    "vad": {"enabled": true},
    "align": {"provider": "whisperx", "model": "wav2vec2-auto", "options": {"device": "cuda"}},
    "diarize": {"provider": "pyannote", "device": "cuda",
                "model": "pyannote/speaker-diarization-community-1"}}
   JSON
   ```

3. Hold the worker's GPU admission lock while the benchmark runs. New worker
   submissions then return 503 with `Retry-After: 120` and the controller retries
   them later; accepted jobs are unaffected. Find the worker database path with
   `docker inspect localplaud-localplaud-gpu-1` (the lock is
   `<database>.gpu-maintenance.lock` on the host side of the bind mount).

   ```sh
   LOCK=~/localplaud/data/localplaud.db.gpu-maintenance.lock
   flock -xn "$LOCK" docker run --rm --gpus all \
     -v ~/localplaud-bench/src:/bench-src:ro -e PYTHONPATH=/bench-src \
     -v ~/lp-bench:/bench -v <model-cache-volume>:/home/localplaud/.cache \
     -e LOCALPLAUD_CONFIG=/bench/empty.toml \
     -e LOCALPLAUD_STORE__DATABASE_URL=sqlite:////bench/bench.db \
     -e LOCALPLAUD_DIARIZE__HF_TOKEN \
     --entrypoint /opt/venv/bin/python <worker-image> -m localplaud.cli \
     benchmark-speech /bench/cohort/manifest.json \
       --speech-config /bench/speech-cuda.json \
       --output /bench/wsl-cuda.json --save-transcripts /bench/wsl-cuda-hyp
   ```

   `PYTHONPATH` makes the branch's source shadow the package installed in the
   image without touching the worker's own mounts. Create `~/lp-bench/empty.toml`
   first, export `LOCALPLAUD_DIARIZE__HF_TOKEN`, and take the image name and model
   cache volume from `docker inspect localplaud-localplaud-gpu-1`. The image runs
   as uid 1000, so `~/lp-bench` must be writable by that user. If `flock -xn` fails, the
   watchdog or an admission is holding the lock; wait and retry rather than
   removing the file.
4. Record the report's aggregate table, `real_time_factor` and the absence of
   CUDA errors or CPU fallback in the run output. Expect `diarization_complete`
   true for every recording and word timing coverage near 100%.
5. Repeat with the owned cohort (step 4 below) before treating the result as
   validation. VRAM is not measured by the harness; watch `nvidia-smi` in a
   second terminal if capacity matters.

## 3. Mac↔WSL artifact contract

1. On the Mac, use a separate worktree, not the production checkout, with its
   own environment and an empty config, and make sure no recording is processing
   (or put the controller in maintenance first, see [maintenance.md](maintenance.md)):

   ```sh
   git worktree add ~/lp-bench/src <branch>
   cd ~/lp-bench/src && uv sync --extra mlx --extra forced-align --extra diarize --extra vad
   cat > ~/lp-bench/speech-mac.json <<'JSON'
   {"asr": {"provider": "mlx-whisper", "language": "zh",
            "mlx_whisper": {"model": "mlx-community/whisper-large-v3-turbo"}},
    "vad": {"enabled": true},
    "align": {"provider": "whisperx", "model": "wav2vec2-auto", "options": {"device": "cpu"}},
    "diarize": {"provider": "pyannote", "device": "auto",
                "model": "pyannote/speaker-diarization-community-1"}}
   JSON
   touch ~/lp-bench/empty.toml   # no production config, Plaud tokens or providers
   LOCALPLAUD_CONFIG=~/lp-bench/empty.toml LOCALPLAUD_STORE__DATABASE_URL=sqlite:///$HOME/lp-bench/bench.db \
   LOCALPLAUD_DIARIZE__HF_TOKEN=hf_... \
     uv run localplaud benchmark-speech ~/lp-bench/cohort/manifest.json \
       --speech-config ~/lp-bench/speech-mac.json \
       --output ~/lp-bench/mac.json --save-transcripts ~/lp-bench/mac-hyp
   ```

   The worktree has no `.env`, so pass the Hugging Face token for the gated
   pyannote model explicitly. The harness rejects any non-local speech selection.
2. Copy `wsl-cuda.json` and `wsl-cuda-hyp/` from the WSL host into `~/lp-bench/`
   (for example with `scp` over Tailscale) and compare:

   ```sh
   uv run localplaud benchmark-speech-compare \
     --baseline ~/lp-bench/mac.json --candidate ~/lp-bench/wsl-cuda.json \
     --hypotheses-a ~/lp-bench/mac-hyp --hypotheses-b ~/lp-bench/wsl-cuda-hyp \
     --output ~/lp-bench/mac-vs-wsl.json
   ```

   `COMPARABLE` means the two hosts produce interchangeable artifacts within the
   gates above; `DIVERGED` lists the recordings and checks that differ. A
   different speaker count between hosts is reported but is not on its own a
   failure. Exit status is 1 when either comparison fails.

## 4. Owned Taiwan Mandarin cohort (production evidence)

Select 10–20 owned recordings that cover: two-person and multi-person meetings,
mixed Mandarin/English, quiet speech, long silence or music, phone or far-field
audio, and at least one recording over an hour. Write full Taiwan Mandarin
references (`text_file`), RTTM speaker turns for at least a subset, and
`non_speech` regions for the silent parts. Mark each entry `"owned": true`.
Run steps 2 and 3 on this manifest, then compare any proposed default (for
example VAD on vs off, or `whisperx` vs `provider-word-timestamps`) against the
current production configuration with `--baseline/--candidate` on the same host.
Record the aggregate tables and the comparison verdict in `TODO.md` before
changing a default.
