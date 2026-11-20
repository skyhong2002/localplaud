#!/usr/bin/env python3
"""Build a synthetic Taiwan Mandarin speech cohort for ``localplaud benchmark-speech``.

Each dialogue script (see ``zh_tw_cohort/*.json``) lists speaker turns, optional
overlaps and low-noise pauses. Every turn is synthesized, placed on one 16 kHz
mono timeline, and the exact reference is written next to the audio:

* full reference text, per-turn segments and RTTM-style speaker turns;
* word timings when the TTS backend reports word boundaries;
* ``non_speech`` regions for the inserted pauses (hallucination checks).

The result is labelled ``source: synthetic``. It validates the benchmark tooling,
alignment/diarization plumbing and coarse regressions. It is **not** evidence for
changing a production default: TTS speech is cleaner and more regular than real
meetings. Use owned recordings for that decision (docs/speech-quality.md).

Backends:

``edge-tts``  Microsoft neural zh-TW voices. Sends only the script text in these
              files to Microsoft's TTS service, so it requires ``--allow-network-tts``.
              Run with ``uv run --with edge-tts==7.2.3 python scripts/benchmark/...``.
``command``   Any local TTS command that reads text on stdin and writes a WAV to
              ``{out}``, for example ``piper --model zh.onnx --output_file {out}``.
              Voices map to ``{voice}``. No word timings are produced.

ffmpeg must be on PATH (or pass ``--ffmpeg``) to decode synthesized clips.

Usage::

    uv run --with edge-tts==7.2.3 python scripts/benchmark/synthesize_speech_cohort.py \\
        --out /tmp/lp-bench/cohort --backend edge-tts --allow-network-tts
"""

from __future__ import annotations

import argparse
import array
import asyncio
import hashlib
import json
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
import wave
from datetime import UTC, datetime
from pathlib import Path

RATE = 16_000
HERE = Path(__file__).resolve().parent
DEFAULT_SCRIPTS = sorted((HERE / "zh_tw_cohort").glob("*.json"))
TURN_GAP_SECONDS = 0.45
LEAD_SECONDS = 0.5


def decode(ffmpeg: str, source: Path) -> array.array:
    """Decode any clip to 16 kHz mono signed 16-bit samples."""
    raw = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(source), "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"],
        check=True, capture_output=True,
    ).stdout
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


async def _edge_tts(text: str, voice: str, destination: Path) -> list[dict]:
    import edge_tts

    communicate = edge_tts.Communicate(text, voice, boundary="WordBoundary")
    audio, words = bytearray(), []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            start = chunk["offset"] / 1e7
            words.append({"text": chunk["text"], "start": start, "end": start + chunk["duration"] / 1e7})
    destination.write_bytes(bytes(audio))
    return words


def synthesize(backend: str, text: str, voice: str, destination: Path, command: str | None) -> list[dict]:
    if backend == "edge-tts":
        return asyncio.run(_edge_tts(text, voice, destination))
    argv = [part.replace("{out}", str(destination)).replace("{voice}", voice) for part in shlex.split(command or "")]
    subprocess.run(argv, input=text.encode(), check=True, capture_output=True)
    return []


def trimmed_bounds(samples: array.array, threshold: int = 300) -> tuple[int, int]:
    loud = [i for i in range(0, len(samples), 160) if abs(samples[i]) > threshold]
    if not loud:
        return 0, len(samples)
    return loud[0], min(len(samples), loud[-1] + 160)


def noise(seconds: float, dbfs: float, seed: int) -> array.array:
    rng = random.Random(seed)
    amplitude = 32767 * 10 ** (dbfs / 20)
    return array.array("h", (int(max(-32767, min(32767, rng.gauss(0, amplitude)))) for _ in range(int(seconds * RATE))))


def mix_into(timeline: array.array, clip: array.array, offset: int) -> None:
    end = offset + len(clip)
    if end > len(timeline):
        timeline.extend([0] * (end - len(timeline)))
    for i, value in enumerate(clip):
        timeline[offset + i] = max(-32767, min(32767, timeline[offset + i] + value))


def build(script: dict, args, workdir: Path) -> dict:
    timeline = array.array("h", [0] * int(LEAD_SECONDS * RATE))
    cursor = LEAD_SECONDS
    texts, segments, words, turns, non_speech = [], [], [], [], []
    for index, turn in enumerate(script["turns"]):
        if "silence" in turn:
            seconds = float(turn["silence"])
            clip = noise(seconds, float(turn.get("noise_dbfs", -60)), seed=index)
            mix_into(timeline, clip, int(cursor * RATE))
            non_speech.append({"start": round(cursor, 3), "end": round(cursor + seconds, 3)})
            cursor += seconds
            continue
        speaker, text = turn["speaker"], turn["text"]
        clip_path = workdir / f"{script['id']}-{index}.audio"
        boundaries = synthesize(args.backend, text, script["voices"][speaker], clip_path, args.tts_command)
        samples = decode(args.ffmpeg, clip_path)
        first, last = trimmed_bounds(samples)
        start = max(LEAD_SECONDS, cursor - float(turn.get("overlap", 0.0)))
        mix_into(timeline, samples, int(start * RATE))
        speech_start = start + first / RATE
        speech_end = start + last / RATE
        if boundaries:
            speech_start = start + boundaries[0]["start"]
            speech_end = max(speech_end, start + boundaries[-1]["end"])
            words.extend({"text": w["text"], "start": round(start + w["start"], 3), "end": round(start + w["end"], 3)}
                         for w in boundaries)
        texts.append(text)
        segments.append({"text": text, "start": round(speech_start, 3), "end": round(speech_end, 3)})
        turns.append({"start": round(speech_start, 3), "end": round(speech_end, 3), "speaker": speaker})
        cursor = start + len(samples) / RATE + float(turn.get("pause_after", TURN_GAP_SECONDS))
    mix_into(timeline, array.array("h", [0] * int(LEAD_SECONDS * RATE)), len(timeline))
    audio_path = args.out / f"{script['id']}.wav"
    with wave.open(str(audio_path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(RATE)
        frames = array.array("h", timeline)
        if sys.byteorder != "little":
            frames.byteswap()
        stream.writeframes(frames.tobytes())
    reference = {"text": "\n".join(texts), "segments": segments, "speaker_turns": turns, "non_speech": non_speech}
    if words:
        reference["words"] = words
    return {"id": script["id"], "source": "synthetic", "audio": audio_path.name, "description": script.get("description"),
            "language": script.get("language"), "duration_seconds": round(len(timeline) / RATE, 3),
            "reference": reference}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="Output directory (created; must be empty or new).")
    parser.add_argument("--scripts", type=Path, nargs="*", default=DEFAULT_SCRIPTS)
    parser.add_argument("--backend", choices=("edge-tts", "command"), required=True)
    parser.add_argument("--allow-network-tts", action="store_true",
                        help="Acknowledge that edge-tts sends the script text to Microsoft's TTS service.")
    parser.add_argument("--tts-command", help="Local command for --backend command; {out} and {voice} are replaced.")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg"), help="ffmpeg executable.")
    args = parser.parse_args(argv)
    if args.backend == "edge-tts" and not args.allow_network_tts:
        parser.error("edge-tts sends the script text to a cloud service; pass --allow-network-tts to confirm")
    if args.backend == "command" and not args.tts_command:
        parser.error("--backend command needs --tts-command")
    if not args.ffmpeg:
        parser.error("ffmpeg was not found; install it or pass --ffmpeg")
    if not args.scripts:
        parser.error("no dialogue scripts given")
    args.out.mkdir(parents=True, exist_ok=True)
    if any(args.out.iterdir()):
        parser.error(f"{args.out} is not empty")
    recordings, digests = [], {}
    with tempfile.TemporaryDirectory(prefix="lp-cohort-") as tmp:
        for path in args.scripts:
            script = json.loads(path.read_text())
            digests[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            recordings.append(build(script, args, Path(tmp)))
            print(f"{script['id']}: {recordings[-1]['duration_seconds']:.1f}s", file=sys.stderr)
    manifest = {"version": 1, "recordings": [
        {k: v for k, v in row.items() if k in {"id", "source", "audio", "reference"}} for row in recordings]}
    (args.out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    provenance = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "backend": args.backend,
        "tts_command": args.tts_command,
        "sample_rate": RATE,
        "scripts": digests,
        "recordings": [{k: row[k] for k in ("id", "description", "language", "duration_seconds")} for row in recordings],
        "evidence": "synthetic speech: validates tooling and coarse regressions, never a production default",
        "timing_note": "word times come from TTS word boundaries; codec padding can shift them by tens of ms",
    }
    if args.backend == "edge-tts":
        try:
            from importlib.metadata import version

            provenance["edge_tts_version"] = version("edge-tts")
        except Exception:  # pragma: no cover - metadata is informational
            pass
    (args.out / "PROVENANCE.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=1) + "\n")
    total = sum(r["duration_seconds"] for r in recordings)
    print(f"wrote {len(recordings)} recordings, {total:.1f}s of audio, to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
