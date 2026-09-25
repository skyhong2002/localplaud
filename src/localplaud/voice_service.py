"""Resumable whole-library voice enrollment/matching and unattended discovery.

Run with an explicit private JSON profile. Only the configured SSH worker sees
speech clips; no provider fallback, API key, or Plaud AI generation is involved.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import io
import json
import os
import select as io_select
import shlex
import subprocess
import tempfile
import time
import uuid
import wave
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import exists, select

from .config import get_settings
from .db.models import PlaudFile, Transcript
from .db.session import get_engine, session_scope
from .voice_identity import VoiceSample, apply_match, create_schema, inventory, references
from .voice_matching import MODEL, REVISION, VoiceMatcher, cosine


class VoiceWorker:
    def __init__(self, profile, log_path):
        self.profile = profile
        self.process = None
        self.log = open(log_path, "a")

    def reset(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    def close(self):
        self.reset()
        self.log.close()

    def read(self, timeout=180):
        if not io_select.select([self.process.stdout], [], [], timeout)[0]:
            raise TimeoutError("voice worker response timeout")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("voice worker stopped; inspect private runtime log")
        return json.loads(line)

    def start(self):
        if self.process and self.process.poll() is None:
            return
        remote = [
            "docker",
            "exec",
            "-i",
            self.profile["container"],
            self.profile["python"],
            "-m",
            "localplaud.voice_runtime",
        ]
        self.process = subprocess.Popen(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=15",
                self.profile["ssh_host"],
                shlex.join(remote),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            bufsize=1,
        )
        ready = self.read(240)
        if ready.get("model") != MODEL or ready.get("revision") != REVISION:
            raise RuntimeError("voice worker model capability mismatch")

    def embed(self, wav, windows):
        request_id = uuid.uuid4().hex
        try:
            self.start()
            self.process.stdin.write(
                json.dumps(
                    {
                        "request_id": request_id,
                        "wav": base64.b64encode(wav).decode(),
                        "windows": windows,
                    }
                )
                + "\n"
            )
            self.process.stdin.flush()
            result = self.read()
            if result.get("request_id") != request_id:
                raise RuntimeError("voice response request mismatch")
            if result.get("error"):
                raise RuntimeError("voice embedding failed: " + result["error"])
            if result.get("model") != MODEL or result.get("revision") != REVISION:
                raise RuntimeError("voice model provenance mismatch")
            vectors = result.get("vectors", [])
            if len(vectors) != len(windows):
                raise RuntimeError("incomplete voice embeddings")
            for vector in vectors:
                if vector is not None:
                    if len(vector) != 192:
                        raise ValueError("unexpected voice embedding dimension")
                    cosine(vector, vector)
            return vectors
        except Exception:
            # Never consume a delayed response as the next recording's vectors.
            self.reset()
            raise


def pack_windows(path, samples):
    """Only selected speech is transmitted; original audio is never modified."""
    output = io.BytesIO()
    packed, owners = [], []
    with wave.open(str(path), "rb") as source, wave.open(output, "wb") as target:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise ValueError("voice audio must be 16k mono PCM16")
        target.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        frame = 0
        for sample in samples:
            for start, end in sample.windows:
                first, last = round(start * 16000), round(end * 16000)
                if last > source.getnframes() or first < 0:
                    continue
                source.setpos(first)
                data = source.readframes(last - first)
                target.writeframes(data)
                packed.append([frame / 16000, (frame + len(data) // 2) / 16000])
                owners.append(sample.id)
                frame += len(data) // 2
    return output.getvalue(), packed, owners


@contextlib.contextmanager
def audio_for(row, settings, profile, client_holder):
    from .plaud import make_plaud_client
    from .plaud.models import PlaudFileDTO
    from .worker.convert import to_wav

    # Temp files are exclusively owned here. Retained originals are read-only.
    with tempfile.TemporaryDirectory(prefix="localplaud-voice-") as directory:
        directory = Path(directory)
        wav = Path(row.wav_path) if row.wav_path else None
        if wav and wav.is_file():
            yield wav
            return
        audio = Path(row.audio_path) if row.audio_path else None
        if audio is None or not audio.is_file():
            if not profile.get("download_missing_audio", False):
                raise FileNotFoundError("raw audio unavailable; downloading is disabled")
            if client_holder[0] is None:
                client_holder[0] = make_plaud_client(settings.plaud)
            dto = PlaudFileDTO.model_validate(row.raw or {"id": row.id})
            audio = client_holder[0].download_audio(dto, directory)
        wav = to_wav(audio, directory / "voice.wav")
        yield wav


def report(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def scan(profile, worker, report_path, *, limit=None):
    from .worker.pipeline import processing_claim_active

    settings = get_settings()
    stats = Counter()
    started = datetime.now(UTC).isoformat()
    with session_scope() as session:
        ids = list(
            session.scalars(
                select(PlaudFile.id)
                .where(PlaudFile.is_trash.is_(False))
                .order_by(
                    (PlaudFile.wav_path.is_not(None) | PlaudFile.audio_path.is_not(None)).desc(),
                    exists(
                        select(Transcript.id).where(
                            Transcript.file_id == PlaudFile.id, Transcript.source == "cloud"
                        )
                    ).desc(),
                    PlaudFile.created_at.desc(),
                )
            )
        )
    active_ids = []
    client_holder = [None]
    for number, fid in enumerate(ids):
        with session_scope() as session:
            row = session.get(PlaudFile, fid)
            if processing_claim_active(row):
                stats["busy_recordings"] += 1
                continue
            samples = inventory(session, row, import_plaud=profile.get("import_plaud_names", False))
            active_ids.extend(s.id for s in samples)
            pending = [
                s
                for s in samples
                if s.status == "pending"
                or (
                    s.status == "failed"
                    and (datetime.now(UTC) - s.updated_at.replace(tzinfo=UTC)).total_seconds()
                    > 3600
                )
            ]
            stats["recordings_scanned"] += 1
        if not pending:
            continue
        if limit is not None and stats["recordings_extracted"] >= limit:
            continue
        try:
            with audio_for(row, settings, profile, client_holder) as wav:
                # The runtime caps requests at 256 windows and 30 minutes.
                for offset in range(0, len(pending), 24):
                    batch = pending[offset : offset + 24]
                    data, windows, owners = pack_windows(wav, batch)
                    vectors = worker.embed(data, windows) if windows else []
                    by_id = {s.id: [] for s in batch}
                    for sid, vector in zip(owners, vectors, strict=True):
                        if vector is not None:
                            by_id[sid].append(vector)
                    with session_scope() as session:
                        for original in batch:
                            sample = session.get(VoiceSample, original.id)
                            sample.vectors = by_id[sample.id]
                            # Mixed/poor diarization must not seed global identities.
                            similarities = [
                                cosine(a, b)
                                for i, a in enumerate(sample.vectors)
                                for b in sample.vectors[i + 1 :]
                            ]
                            consistent = (
                                not similarities
                                or sorted(similarities)[len(similarities) // 2] >= 0.5
                            )
                            sample.status = (
                                "ready"
                                if len(sample.vectors) >= 2 and consistent
                                else "insufficient"
                            )
                            sample.error = None
                            sample.updated_at = datetime.now(UTC)
                            stats["samples_" + sample.status] += 1
            stats["recordings_extracted"] += 1
        except Exception as exc:
            # Errors never include audio, names, URLs, credentials or raw provider text.
            with session_scope() as session:
                for original in pending:
                    sample = session.get(VoiceSample, original.id)
                    if sample.status == "pending" or sample.status == "failed":
                        sample.status, sample.error = "failed", type(exc).__name__
                        sample.updated_at = datetime.now(UTC)
            stats["recordings_failed"] += 1
            print(
                json.dumps({"event": "extraction_failed", "error": type(exc).__name__}), flush=True
            )
        report(
            report_path,
            {
                "started_at": started,
                "phase": "extract",
                "counts": dict(stats),
                "library_recordings": len(ids),
            },
        )
        if number % 10 == 0:
            print(json.dumps({"event": "progress", "counts": dict(stats)}), flush=True)
    with session_scope() as session:
        active = [session.get(VoiceSample, sid) for sid in active_ids]
        refs = references(active)
        targets = [s for s in active if s.source == "local" and s.status == "ready"]
    stats["library_samples_pending"] = sum(s.status == "pending" for s in active)
    stats["library_samples_failed"] = sum(s.status == "failed" for s in active)
    stats["library_samples_ready"] = sum(s.status == "ready" for s in active)
    stats["library_samples_insufficient"] = sum(s.status == "insufficient" for s in active)
    stats["library_recordings_enrolled"] = len({s.file_id for s in active if s.status == "ready"})
    stats["reference_speakers"] = len(refs)
    stats["reference_names"] = len({r["name"] for r in refs})
    stats["query_speakers"] = len(targets)
    threshold, margin = profile.get("threshold", 0.75), profile.get("margin", 0.12)
    # Never use applied results as enrollment. The frozen reference set is rebuilt
    # from explicit manual/opt-in imported references on each full scan.
    matcher = VoiceMatcher(refs)
    for sample in targets:
        decision = matcher.match(
            sample.vectors, threshold=threshold, margin=margin, query_file_id=sample.file_id
        )
        stats["match_" + decision["status"]] += 1
        if profile.get("apply_names", False):
            with session_scope() as session:
                stats[
                    "apply_"
                    + apply_match(session, sample, decision, threshold=threshold, margin=margin)
                ] += 1
    # Cloud labels are a proxy, not ground-truth accuracy. Leave out the entire file.
    evaluation = Counter()
    for ref in refs[:200]:
        result = matcher.match(
            ref["vectors"], threshold=threshold, margin=margin, query_file_id=ref["file_id"]
        )
        evaluation["tested"] += 1
        if result["status"] == "matched":
            evaluation["matched"] += 1
            evaluation[
                "label_agreement" if result["name"] == ref["name"] else "label_disagreement"
            ] += 1
    receipt = {
        "started_at": started,
        "completed_at": datetime.now(UTC).isoformat(),
        "phase": "complete",
        "model": MODEL,
        "model_revision": REVISION,
        "counts": dict(stats),
        "plaud_proxy_evaluation": dict(evaluation),
        "auto_apply": profile.get("apply_names", False),
        "threshold": threshold,
        "margin": margin,
    }
    report(report_path, receipt)
    print(json.dumps(receipt), flush=True)
    if client_holder[0] is not None and hasattr(client_holder[0], "close"):
        client_holder[0].close()
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    os.umask(0o077)
    folder = args.profile.resolve().parent
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "voice-service.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("voice service already running") from None
        create_schema(get_engine())
        while True:
            profile = json.loads(args.profile.read_text())
            if not profile.get("enabled", False):
                if not args.watch:
                    return
                time.sleep(30)
                continue
            if (
                not 0.6 <= profile.get("threshold", 0.75) <= 1
                or not 0.05 <= profile.get("margin", 0.12) <= 0.5
            ):
                raise ValueError("unsafe voice matching threshold or margin")
            worker = VoiceWorker(profile, folder / "runtime.log")
            try:
                scan(profile, worker, folder / "receipt.json", limit=args.limit)
            except Exception as exc:
                print(json.dumps({"event": "scan_failed", "error": type(exc).__name__}), flush=True)
                if not args.watch:
                    raise
            finally:
                worker.close()
            if not args.watch:
                break
            time.sleep(max(30, profile.get("poll_seconds", 300)))


if __name__ == "__main__":
    main()
