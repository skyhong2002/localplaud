"""Local-only speech evaluation; metrics require no inference dependencies.

Times are seconds. MER means mixed error rate: Mandarin characters and English
words are scoring units. No inferred Mandarin word segmentation is claimed.
"""
from __future__ import annotations

import difflib
import json
import math
import multiprocessing
import re
import resource
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave
from dataclasses import asdict
from pathlib import Path

from .asr.base import Transcript
from .config import Settings

_LOCAL_ASR = {"mlx-whisper", "faster-whisper", "whispercpp", "qwen"}
_LOCAL_ALIGN = _LOCAL_ASR | {"provider-word-timestamps", "whisperx"}


def normalize(text: str) -> str:
    """Ignore punctuation/case/spacing; preserve Traditional/Simplified differences."""
    return unicodedata.normalize("NFKC", text).casefold()


def mixed_tokens(text: str) -> list[str]:
    return re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|[^\W_\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+(?:'[^\W_\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+)?", normalize(text))


def characters(text: str) -> list[str]:
    return [c for c in normalize(text) if c.isalnum()]


def edit_counts(reference: list[str], hypothesis: list[str]) -> dict:
    """Levenshtein counts, using linear memory; deterministic substitution ties."""
    row = [(i, 0, i, 0, 0) for i in range(len(hypothesis) + 1)]
    for i, token in enumerate(reference, 1):
        new = [(i, 0, 0, i, 0)]
        for j, other in enumerate(hypothesis, 1):
            cost, sub, ins, delete, hits = row[j - 1]
            diagonal = (cost + (token != other), sub + (token != other), ins, delete, hits + (token == other))
            cost, sub, ins, delete, hits = row[j]
            deletion = (cost + 1, sub, ins, delete + 1, hits)
            cost, sub, ins, delete, hits = new[j - 1]
            insertion = (cost + 1, sub, ins + 1, delete, hits)
            new.append(min((diagonal, deletion, insertion), key=lambda item: item[0]))
        row = new
    errors, substitutions, insertions, deletions, hits = row[-1]
    return {"errors": errors, "substitutions": substitutions, "insertions": insertions,
            "deletions": deletions, "hits": hits, "reference_units": len(reference),
            "rate": errors / len(reference) if reference else (0.0 if not hypothesis else None)}


def text_metrics(reference: str, hypothesis: str) -> dict:
    mixed = edit_counts(mixed_tokens(reference), mixed_tokens(hypothesis))
    return {"cer": edit_counts(characters(reference), characters(hypothesis)),
            "wer": dict(mixed), "mer": dict(mixed)}


def _interval(item: dict) -> tuple[float, float]:
    if any(isinstance(item[key], bool) or not isinstance(item[key], int | float) for key in ("start", "end")):
        raise ValueError("times must be numeric seconds")
    start, end = float(item["start"]), float(item["end"])
    if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end:
        raise ValueError("reference/hypothesis times must be finite, ordered, positive seconds")
    return start, end


def parse_rttm(text: str, recording_id: str | None = None) -> list[dict]:
    turns = []
    identities = set()
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 9 or fields[0] != "SPEAKER":
            raise ValueError("expected RTTM SPEAKER rows")
        identities.add(fields[1])
        if recording_id is not None and fields[1] != recording_id:
            continue
        start, duration = float(fields[3]), float(fields[4])
        turn = {"start": start, "end": start + duration, "speaker": fields[7]}
        _interval(turn)
        turns.append(turn)
    if recording_id is None and len(identities) > 1:
        raise ValueError("multi-recording RTTM requires rttm_recording_id")
    return turns


def _assignment(weights: list[list[float]]) -> dict[int, int]:
    """Maximum-weight speaker assignment via the Hungarian algorithm."""
    n = len(weights)
    if not n:
        return {}
    u, v, p, way = [0.0] * (n + 1), [0.0] * (n + 1), [0] * (n + 1), [0] * (n + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minimum, used = [float("inf")] * (n + 1), [False] * (n + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], float("inf"), 0
            for j in range(1, n + 1):
                if not used[j]:
                    current = -weights[i0 - 1][j - 1] - u[i0] - v[j]
                    if current < minimum[j]:
                        minimum[j], way[j] = current, j0
                    if minimum[j] < delta:
                        delta, j1 = minimum[j], j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    return {p[j] - 1: j - 1 for j in range(1, n + 1)}


def diarization_error(reference: list[dict], hypothesis: list[dict]) -> dict:
    """Continuous-time DER, zero collar, overlaps scored, optimal speaker mapping."""
    for turn in reference + hypothesis:
        _interval(turn)
    refs = sorted({t["speaker"] for t in reference})
    hyps = sorted({t["speaker"] for t in hypothesis})
    n = max(len(refs), len(hyps))
    weights = [[0.0] * n for _ in range(n)]
    bounds = sorted({x for t in reference + hypothesis for x in _interval(t)})
    intervals = []
    for start, end in zip(bounds, bounds[1:], strict=False):
        r = {t["speaker"] for t in reference if t["start"] <= start < t["end"]}
        h = {t["speaker"] for t in hypothesis if t["start"] <= start < t["end"]}
        duration = end - start
        intervals.append((r, h, duration))
        for a in r:
            for b in h:
                weights[refs.index(a)][hyps.index(b)] += duration
    assignment = _assignment(weights)
    mapping = {hyps[j]: refs[i] for i, j in assignment.items() if i < len(refs) and j < len(hyps)}
    missed = false_alarm = confusion = denominator = 0.0
    for r, h, duration in intervals:
        correct = len(r & {mapping.get(s) for s in h})
        denominator += len(r) * duration
        missed += max(0, len(r) - len(h)) * duration
        false_alarm += max(0, len(h) - len(r)) * duration
        confusion += (min(len(r), len(h)) - correct) * duration
    errors = missed + false_alarm + confusion
    return {"rate": errors / denominator if denominator else (0.0 if not errors else None),
            "reference_speaker_seconds": denominator, "missed_seconds": missed,
            "false_alarm_seconds": false_alarm, "confusion_seconds": confusion,
            "mapping": mapping, "collar_seconds": 0, "score_overlap": True}


def hallucination_indicators(segments: list[dict], non_speech: list[dict] | None) -> dict:
    tokens = mixed_tokens(" ".join(s["text"] for s in segments))
    loops = []
    for width in range(1, 9):
        i = 0
        while i + 3 * width <= len(tokens):
            unit = tokens[i:i + width]
            repeats = 1
            while tokens[i + repeats * width:i + (repeats + 1) * width] == unit:
                repeats += 1
            if repeats >= 3:
                loops.append({"token_start": i, "ngram_size": width, "repeats": repeats})
                i += repeats * width
            else:
                i += 1
    flagged = []
    if non_speech is not None:
        intervals = sorted(_interval(region) for region in non_speech)
        merged = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for i, segment in enumerate(segments):
            start, end = _interval(segment)
            overlap = sum(max(0, min(end, b) - max(start, a)) for a, b in merged)
            if segment["text"].strip() and overlap > 0:
                flagged.append({"segment": i, "overlap_seconds": overlap, "fraction": overlap / (end - start)})
    return {"repeated_ngram_loops": loops, "non_speech_regions_available": non_speech is not None,
            "text_in_non_speech": flagged if non_speech is not None else None}


def timestamp_deviation(reference: list[dict], hypothesis: list[dict]) -> dict:
    """Align equal lexical units monotonically, then compare their boundaries."""
    def expand(items):
        return [(token, *_interval(item)) for item in items for token in mixed_tokens(item["text"])]
    r, h = expand(reference), expand(hypothesis)
    # Matching blocks avoid a quadratic-memory word alignment for long meetings.
    matcher = difflib.SequenceMatcher(None, [x[0] for x in r], [x[0] for x in h], autojunk=False)
    errors = []
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            a, b = r[block.a + offset], h[block.b + offset]
            errors.extend((abs(a[1] - b[1]), abs(a[2] - b[2])))
    return {"matched_units": len(errors) // 2, "reference_units": len(r),
            "absolute_error_sum_seconds": sum(errors), "boundary_count": len(errors),
            "mean_absolute_seconds": sum(errors) / len(errors) if errors else None,
            "max_absolute_seconds": max(errors) if errors else None}


def score_recording(reference: dict, transcript: Transcript) -> dict:
    segments = [asdict(s) for s in transcript.segments]
    result = text_metrics(reference["text"], transcript.text)
    result["hallucination"] = hallucination_indicators(segments, reference.get("non_speech"))
    words = [asdict(w) | {"speaker": w.speaker or s.speaker} for s in transcript.segments for w in s.words]
    timed = reference.get("words") or reference.get("segments")
    result["timestamps"] = timestamp_deviation(timed, words if reference.get("words") else segments) if timed else None
    result["der"] = diarization_error(reference["speaker_turns"], [
        {"start": s["start"], "end": s["end"], "speaker": s["speaker"]}
        for s in (words or segments) if s.get("speaker")
    ]) if "speaker_turns" in reference else None
    return result


def validate_local(settings: Settings, alignment: dict) -> None:
    if settings.asr.provider not in _LOCAL_ASR or any(p not in _LOCAL_ASR for p in settings.asr.fallback):
        raise ValueError("benchmark-speech never uploads audio: only local ASR is allowed")
    if alignment["provider"] not in _LOCAL_ALIGN:
        raise ValueError("benchmark-speech requires local alignment")
    if settings.diarize.provider not in {"pyannote", "nemotron", "none"}:
        raise ValueError("benchmark-speech requires local diarization")


def configuration(settings: Settings, *, profile_id: int | None = None, explicit: dict | None = None) -> tuple[Settings, dict, dict]:
    """Read a selected profile without bootstrapping or mutating the database."""
    result = settings.model_copy(deep=True)
    alignment = {"provider": "provider-word-timestamps", "model": None, "options": {}}
    provenance = {"source": "explicit-config"}
    if profile_id is not None:
        if explicit:
            raise ValueError("choose an execution profile OR explicit speech configuration")
        from sqlalchemy.orm import Session

        from .db.models import ExecutionProfile
        from .db.session import get_engine
        from .providers.resolver import resolve_profile
        from .providers.service import _capability_catalog, _connection_catalog, _profile_layer
        from .worker.pipeline import _settings_for_stage

        with Session(get_engine()) as session:
            profile = session.get(ExecutionProfile, profile_id)
            if profile is None:
                raise ValueError("execution profile not found")
            snapshot = resolve_profile([_profile_layer(profile)], _capability_catalog(session), _connection_catalog(session)).to_dict()
        for stage in ("transcribe", "align", "diarize"):
            selection = snapshot["stages"].get(stage)
            if not selection:
                raise ValueError(f"profile requires an explicit {stage} selection")
            if selection.get("data_egress") or selection.get("execution_target") != "local":
                raise ValueError(f"benchmark-speech rejects non-local {stage} selection")
        result = _settings_for_stage(result, snapshot, "transcribe")
        result = _settings_for_stage(result, snapshot, "diarize")
        selected = snapshot["stages"]["align"]
        alignment = {"provider": selected["provider_type"], "model": selected["model"], "options": (selected.get("configuration") or {}) | (selected.get("options") or {})}
        # No fallback execution in evaluation: compare the chosen system honestly.
        provenance = {"source": "execution-profile", "profile_id": profile_id, "layers": snapshot["layers"]}
    else:
        explicit = explicit or {}
        if set(explicit) - {"asr", "vad", "align", "diarize"}:
            raise ValueError("speech config accepts only asr, vad, align and diarize")
        from .config import AsrConfig, DiarizeConfig, VadConfig

        result.asr = AsrConfig.model_validate(result.asr.model_dump() | explicit.get("asr", {}))
        if "vad" in explicit:
            result.asr.vad = VadConfig.model_validate(explicit["vad"])
        result.diarize = DiarizeConfig.model_validate(result.diarize.model_dump() | explicit.get("diarize", {}))
        alignment |= explicit.get("align", {})
    validate_local(result, alignment)
    result.asr.fallback = []
    result.pipeline.align = True
    result.pipeline.diarize = result.diarize.provider != "none"
    provenance |= {"asr": result.asr.provider, "asr_model": getattr(getattr(result.asr, result.asr.provider.replace("-", "_")), "model", None),
                   "vad": result.asr.vad.model_dump(), "align": alignment["provider"], "align_model": alignment["model"],
                   "diarize": result.diarize.provider, "diarize_model": result.diarize.model,
                   "asr_options": getattr(result.asr, result.asr.provider.replace("-", "_")).model_dump(mode="json"),
                   "alignment_options": alignment["options"],
                   "diarization_options": result.diarize.model_dump(mode="json", exclude={"hf_token"}),
                   "fallback_execution": False}
    return result, alignment, provenance


def _execute_local(audio: Path, settings: Settings, alignment: dict) -> tuple[Transcript, float, dict]:
    from .worker.align import run_alignment
    from .worker.convert import to_wav
    from .worker.diarize import diarize
    from .worker.transcribe import run_asr

    validate_local(settings, alignment)
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="lp-speech-") as directory:
        wav = to_wav(audio, Path(directory) / "input.wav")
        with wave.open(str(wav)) as stream:
            duration = stream.getnframes() / stream.getframerate()
        transcript = run_asr(wav, settings)
        stage_detail = {}
        if settings.pipeline.align:
            aligned = run_alignment(wav, transcript, **alignment)
            transcript = aligned.transcript
            stage_detail["align"] = {"provider": aligned.provider, "model": aligned.model, "detail": aligned.detail}
        if settings.pipeline.diarize:
            transcript = diarize(wav, transcript, settings.diarize)
        stage_detail["diarization_complete"] = transcript.has_speakers
    return transcript, duration, {"elapsed_seconds": time.perf_counter() - start, "stages": stage_detail}


def _child(pipe, audio, settings, alignment):
    try:
        # Avoid Qwen checkpoints mutating the library; each evaluation is isolated.
        with tempfile.TemporaryDirectory(prefix="lp-speech-checkpoints-") as directory:
            settings.asr.qwen.checkpoint_dir = directory
            transcript, duration, execution = _execute_local(audio, settings, alignment)
        scale = 1 if sys.platform == "darwin" else 1024
        execution["peak_process_memory_bytes"] = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) * scale
        pipe.send((transcript, duration, execution, None))
    except Exception as exc:
        from .error_redaction import sanitize_error

        pipe.send((None, None, None, sanitize_error(str(exc))))
    finally:
        pipe.close()


def _tree_rss(pid: int) -> int:
    """Sample CPU RSS for the spawned process and all descendants (ps KiB)."""
    output = subprocess.run(["ps", "-axo", "pid=,ppid=,rss="], capture_output=True, text=True, check=True).stdout
    entries = [tuple(map(int, line.split())) for line in output.splitlines() if len(line.split()) == 3]
    descendants = {pid}
    while True:
        found = descendants | {p for p, parent, _ in entries if parent in descendants}
        if found == descendants:
            break
        descendants = found
    return sum(rss * 1024 for p, _, rss in entries if p in descendants)


def execute_isolated(audio: Path, settings: Settings, alignment: dict) -> tuple[Transcript, float, dict]:
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_child, args=(writer, audio, settings, alignment))
    process.start()
    writer.close()
    peak = 0
    try:
        while not reader.poll(0.1):
            peak = max(peak, _tree_rss(process.pid))
            if not process.is_alive():
                raise RuntimeError(f"speech benchmark subprocess exited {process.exitcode}")
        transcript, duration, execution, error = reader.recv()
        if error:
            raise RuntimeError(error)
        execution["peak_memory_bytes"] = max(peak, execution["peak_process_memory_bytes"])
        execution["memory_scope"] = "sampled process-tree CPU RSS, 100ms; process high-water lower bound; excludes GPU VRAM"
        return transcript, duration, execution
    finally:
        reader.close()
        if process.is_alive():
            process.join(timeout=2)
        if process.is_alive():
            process.terminate()
        process.join()


def load_manifest(path: Path) -> list[dict]:
    payload = json.loads(path.read_text())
    if payload.get("version") != 1 or not isinstance(payload.get("recordings"), list) or not payload["recordings"]:
        raise ValueError("manifest requires version 1 and non-empty recordings")
    items, seen = [], set()
    for row in payload["recordings"]:
        if row.get("owned") is not True:
            raise ValueError("every recording must declare owned: true")
        identity = str(row["id"])
        if identity in seen:
            raise ValueError("recording ids must be unique")
        seen.add(identity)
        reference = dict(row["reference"])
        if "text_file" in reference:
            reference["text"] = (path.parent / reference.pop("text_file")).read_text()
        if not isinstance(reference.get("text"), str):
            raise ValueError("reference requires text or text_file")
        if "rttm" in reference:
            reference["speaker_turns"] = parse_rttm((path.parent / reference.pop("rttm")).read_text(), reference.get("rttm_recording_id"))
        for group in ("words", "segments", "non_speech", "speaker_turns"):
            for entry in reference.get(group, []):
                _interval(entry)
        audio = path.parent / row["audio"]
        if not audio.is_file():
            raise ValueError(f"recording {identity} audio does not exist")
        items.append({"id": identity, "audio": audio, "reference": reference})
    return items


def aggregate(records: list[dict]) -> dict:
    good = [r for r in records if r.get("metrics")]
    result = {"completed": len(good), "failed": len(records) - len(good)}
    for name in ("cer", "wer", "mer"):
        counts = {key: sum(r["metrics"][name][key] for r in good) for key in ("errors", "reference_units", "substitutions", "insertions", "deletions", "hits")}
        counts["rate"] = counts["errors"] / counts["reference_units"] if counts["reference_units"] else (0.0 if not counts["errors"] else None)
        result[name] = counts
    ders = [r["metrics"]["der"] for r in good if r["metrics"]["der"] is not None]
    denominator = sum(d["reference_speaker_seconds"] for d in ders)
    errors = sum(d["missed_seconds"] + d["false_alarm_seconds"] + d["confusion_seconds"] for d in ders)
    result["der"] = {"rate": errors / denominator if denominator else (0.0 if ders and not errors else None), "reference_speaker_seconds": denominator, "errors_seconds": errors, "recordings": len(ders)}
    timed = [r["metrics"]["timestamps"] for r in good if r["metrics"]["timestamps"] is not None]
    boundaries = sum(t["boundary_count"] for t in timed)
    result["timestamps"] = {"mean_absolute_seconds": sum(t["absolute_error_sum_seconds"] for t in timed) / boundaries if boundaries else None, "boundary_count": boundaries, "recordings": len(timed)}
    result["hallucination"] = {"recordings_with_loops": sum(bool(r["metrics"]["hallucination"]["repeated_ngram_loops"]) for r in good), "recordings_with_non_speech_text": sum(bool(r["metrics"]["hallucination"]["text_in_non_speech"]) for r in good), "recordings_with_non_speech_reference": sum(r["metrics"]["hallucination"]["non_speech_regions_available"] for r in good)}
    audio = sum(r["audio_seconds"] for r in good)
    elapsed = sum(r["execution"]["elapsed_seconds"] for r in good)
    result |= {"audio_seconds": audio, "elapsed_seconds": elapsed, "real_time_factor": elapsed / audio if audio else None,
               "peak_memory_bytes": max((r["execution"]["peak_memory_bytes"] for r in good), default=None)}
    return result


def benchmark(manifest: Path, settings: Settings, alignment: dict, provenance: dict, *, executor=execute_isolated) -> dict:
    validate_local(settings, alignment)
    records = []
    for item in load_manifest(manifest):
        row = {"id": item["id"]}
        try:
            transcript, duration, execution = executor(item["audio"], settings, alignment)
            row |= {"metrics": score_recording(item["reference"], transcript), "audio_seconds": duration,
                    "execution": execution, "real_time_factor": execution["elapsed_seconds"] / duration if duration else None,
                    "actual_asr": {"provider": transcript.provider, "model": transcript.model},
                    "processing_metadata": transcript.processing_metadata}
        except Exception as exc:
            from .error_redaction import sanitize_error

            row["error"] = sanitize_error(str(exc))
        records.append(row)
    return {"schema": "localplaud-speech-benchmark/v1", "configuration": provenance, "recordings": records,
            "aggregate": aggregate(records), "metric_policy": {
                "normalization": "NFKC, casefold, punctuation/spacing ignored; Chinese script differences preserved",
                "wer_mer": "Mandarin characters plus English words; MER=mixed error rate; WER uses the same mixed tokens",
                "memory": "CPU RSS process tree sampled at 100ms; excludes GPU VRAM",
                "hallucinations": "indicators only; non-speech annotations required, no inferred truth"}}
