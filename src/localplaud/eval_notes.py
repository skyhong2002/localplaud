"""Deterministic note-fidelity scoring against a human-written answer key.

The key lists what a faithful note must say (facts, decisions, actions with
owners/deadlines), statements that would reverse the recording's meaning, and
text that must never appear (template leakage, invented names). Matching is
lexical: each key item lists alternative term groups, and an item counts as
covered when every term of one group appears in the note. A miss can therefore
be a paraphrase rather than an omission; misses are reported by item ID so a
reviewer can confirm them or add another accepted wording to the key.

Scores describe agreement with the key. They are not an LLM judgement, an
acoustic check, or proof of note quality on recordings outside the key.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from .benchmark_speech import hallucination_indicators

SCHEMA = "localplaud-note-fidelity/v1"
WEIGHTS = {"critical": 3, "major": 2, "minor": 1}
THIRDS = ("early", "middle", "late")
# Proposed review thresholds. They encode docs/note-quality.md targets (95%
# important-fact coverage, zero critical reversals); they are not measured results.
DEFAULT_GATES = {
    "min_critical_coverage": 1.0,
    "min_weighted_coverage": 0.95,
    "min_third_coverage": 0.8,
    "max_contradictions": 0,
    "max_leaks": 0,
    "max_action_attribution_errors": 0,
    "max_repeated_lines": 0,
}

_TXT_LINE = re.compile(r"^\[(?:(\d+):)?(\d{1,2}):(\d{2})\]\s*(.*)$")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)、])\s+")
_NUMBER = re.compile(r"\d+(?:[.:/]\d+)*")
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG = {"萬": 10_000, "万": 10_000, "億": 100_000_000, "亿": 100_000_000}
_CN_RUN = re.compile(r"[零〇一二兩两三四五六七八九十百千萬万億亿]+")
_CN_DECIMAL = re.compile(r"([零〇一二兩两三四五六七八九十百千]+)[點点]([零〇一二三四五六七八九]+)")


def normalize(text: str) -> str:
    """NFKC, casefold and collapse whitespace; Chinese script differences are kept."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def _chinese_number(run: str, *, loose: bool = False) -> int | None:
    if not any(ch in _CN_DIGITS or ch in _CN_UNITS for ch in run):
        return None
    if not loose and run in {"一", "兩", "两"}:
        # 一起, 一定, 兩邊: too common in prose to treat as a claimed number.
        return None
    total = section = digit = 0
    for ch in run:
        if ch in _CN_DIGITS:
            digit = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            section += (digit or 1) * _CN_UNITS[ch]
            digit = 0
        else:
            total += (section + digit) * _CN_BIG[ch]
            section = digit = 0
    return total + section + digit


def numbers(text: str, *, loose: bool = False) -> set[str]:
    """Numeric values in text, with simple Chinese numerals converted to digits.

    Ordered-list markers are ignored. Units such as 萬 are folded into the value
    so 三百萬 and 300萬 and 3000000 compare equal, and 零點五 equals 0.5.
    ``loose`` also reads a lone 一/兩, which suits the reference transcript.
    """
    lines = [_LIST_MARKER.sub("", line) for line in unicodedata.normalize("NFKC", text).splitlines()]
    # NFKC turns the full-width comma into ","; drop only thousands separators.
    body = re.sub(r"(?<=\d),(?=\d{3}\b)", "", "\n".join(lines)).replace("百分之", " ")
    found: set[str] = set()
    for match in _CN_DECIMAL.finditer(body):
        whole = _chinese_number(match.group(1), loose=True) or 0
        fraction = "".join(str(_CN_DIGITS[ch]) for ch in match.group(2))
        found.add(_canonical(float(f"{whole}.{fraction}")))
    body = _CN_DECIMAL.sub(" ", body)
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*([萬万億亿])", body):
        found.add(_canonical(float(match.group(1)) * _CN_BIG[match.group(2)]))
    for match in _NUMBER.finditer(body):
        value = match.group(0)
        if re.match(r"\s*[萬万億亿]", body[match.end():]):
            continue
        found.add(_canonical(float(value)) if re.fullmatch(r"\d+(?:\.\d+)?", value) else value)
    for match in _CN_RUN.finditer(body):
        value = _chinese_number(match.group(0), loose=loose)
        if value is not None:
            found.add(_canonical(value))
    return found


def _canonical(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def parse_transcript(text: str, suffix: str) -> list[dict]:
    """Segments from a JSON transcript or the Web App's timestamped TXT export."""
    if suffix == ".json":
        payload = json.loads(text)
        segments = payload["segments"] if isinstance(payload, dict) else payload
        result = []
        for item in segments:
            start = float(item["start"])
            end = float(item.get("end", start))
            result.append({"start": start, "end": end, "speaker": item.get("speaker"), "text": str(item["text"])})
        return result
    result = []
    for line in text.splitlines():
        match = _TXT_LINE.match(line.strip())
        if not match:
            continue
        hours, minutes, seconds, body = match.groups()
        start = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
        result.append({"start": float(start), "end": float(start), "speaker": None, "text": body})
    for current, following in zip(result, result[1:], strict=False):
        current["end"] = max(current["start"], following["start"])
    if not result:
        raise ValueError("transcript TXT needs the Web App export with timestamps ([mm:ss] lines)")
    return result


def _groups(item: dict) -> list[list[str]]:
    groups = item.get("match")
    if not isinstance(groups, list) or not groups:
        raise ValueError(f"key item {item.get('id')!r} needs a non-empty match list")
    result = []
    for group in groups:
        terms = [group] if isinstance(group, str) else group
        if not terms or not all(isinstance(term, str) and term.strip() for term in terms):
            raise ValueError(f"key item {item.get('id')!r} has an empty match group")
        result.append(terms)
    return result


def _covered(groups: list[list[str]], text: str) -> list[str] | None:
    folded = normalize(text)
    for terms in groups:
        if all(normalize(term) in folded for term in terms):
            return terms
    return None


def _units(note: str) -> list[str]:
    """Bullets/lines are the scope for owner and deadline attribution."""
    return [line for line in (raw.strip() for raw in note.splitlines()) if line]


def _locate(item: dict, segments: list[dict]) -> float | None:
    if isinstance(item.get("at"), int | float) and not isinstance(item.get("at"), bool):
        return float(item["at"])
    evidence = item.get("evidence")
    for segment in segments:
        if evidence and normalize(evidence) in normalize(segment["text"]):
            return segment["start"]
    for segment in segments:
        if _covered(_groups(item), segment["text"]):
            return segment["start"]
    return None


def _third(position: float | None, duration: float) -> str | None:
    if position is None or duration <= 0:
        return None
    return THIRDS[min(2, int(3 * position / duration))]


def _repeated_lines(note: str) -> list[str]:
    seen: dict[str, int] = {}
    for line in _units(note):
        key = normalize(_LIST_MARKER.sub("", line))
        if len(key) >= 8 and not key.startswith("#"):
            seen[key] = seen.get(key, 0) + 1
    return [line for line, count in seen.items() if count > 1]


def score_note(note: str, segments: list[dict], key: dict, gates: dict | None = None) -> dict:
    gates = DEFAULT_GATES | (gates or {})
    transcript_text = "\n".join(s["text"] for s in segments)
    duration = max((s["end"] for s in segments), default=0.0)
    units = _units(note)
    items, weights, hits = [], {t: 0 for t in THIRDS}, {t: 0 for t in THIRDS}
    total_weight = covered_weight = critical = critical_hit = 0
    attribution_errors = 0
    for item in key.get("items", []):
        identity = str(item["id"])
        importance = item.get("importance", "major")
        if importance not in WEIGHTS:
            raise ValueError(f"key item {identity!r} importance must be one of {sorted(WEIGHTS)}")
        groups = _groups(item)
        matched = _covered(groups, note)
        weight = WEIGHTS[importance]
        position = _locate(item, segments)
        third = _third(position, duration)
        row = {"id": identity, "kind": item.get("kind", "fact"), "importance": importance,
               "covered": matched is not None, "matched_terms": matched,
               "transcript_seconds": position, "third": third}
        if position is None:
            row["warning"] = "not located in the transcript; add evidence or at to the key"
        total_weight += weight
        covered_weight += weight if matched else 0
        if importance == "critical":
            critical += 1
            critical_hit += bool(matched)
        if third:
            weights[third] += weight
            hits[third] += weight if matched else 0
        if matched and (item.get("owner") or item.get("deadline")):
            scope = [unit for unit in units if _covered([matched], unit)]
            checks = {}
            for field in ("owner", "deadline"):
                expected = item.get(field)
                if expected:
                    accepted = [expected] if isinstance(expected, str) else list(expected)
                    checks[field] = any(normalize(term) in normalize(unit) for unit in scope for term in accepted)
            row["attribution"] = checks
            attribution_errors += sum(not ok for ok in checks.values())
        items.append(row)

    contradictions = []
    for item in key.get("contradictions", []):
        matched = _covered(_groups(item), note)
        if matched:
            contradictions.append({"id": str(item["id"]), "matched_terms": matched,
                                   "reason": item.get("reason"), "severity": item.get("severity", "critical")})
    leaks = [term for term in key.get("must_not_contain", []) if normalize(term) in normalize(note)]
    transcript_numbers = numbers(transcript_text, loose=True) | {str(n) for n in key.get("allowed_numbers", [])}
    unsupported = sorted(numbers(note) - transcript_numbers)
    repeated = _repeated_lines(note)
    loops = hallucination_indicators([{"start": 0, "end": 0, "text": note}], None)["repeated_ngram_loops"]

    def ratio(numerator, denominator):
        return numerator / denominator if denominator else None

    third_coverage = {t: ratio(hits[t], weights[t]) for t in THIRDS}
    metrics = {
        "weighted_coverage": ratio(covered_weight, total_weight),
        "critical_coverage": ratio(critical_hit, critical),
        "third_coverage": third_coverage,
        "contradictions": len(contradictions),
        "leaks": len(leaks),
        "action_attribution_errors": attribution_errors,
        "repeated_lines": len(repeated),
        "unsupported_numbers": len(unsupported),
        "note_characters": len(normalize(note)),
        "transcript_characters": len(normalize(transcript_text)),
    }
    metrics["compression_ratio"] = ratio(metrics["note_characters"], metrics["transcript_characters"])
    failures = []

    def below(name, value, limit):
        if value is not None and value < limit:
            failures.append(f"{name} {value:.3f} < {limit}")

    below("critical_coverage", metrics["critical_coverage"], gates["min_critical_coverage"])
    below("weighted_coverage", metrics["weighted_coverage"], gates["min_weighted_coverage"])
    for third in THIRDS:
        below(f"{third}_coverage", third_coverage[third], gates["min_third_coverage"])
    for name in ("contradictions", "leaks", "action_attribution_errors", "repeated_lines"):
        if metrics[name] > gates[f"max_{name}"]:
            failures.append(f"{name} {metrics[name]} > {gates[f'max_{name}']}")
    warnings = []
    if unsupported:
        warnings.append("numbers not found in the transcript: " + ", ".join(unsupported))
    if loops:
        warnings.append(f"{len(loops)} repeated n-gram loop(s) in the note")
    if any("warning" in row for row in items):
        warnings.append("some key items could not be located in the transcript")
    verdict = "fail" if failures else ("review" if warnings else "pass")
    return {"verdict": verdict, "failures": failures, "warnings": warnings, "metrics": metrics,
            "items": items, "missed": [row["id"] for row in items if not row["covered"]],
            "contradictions": contradictions, "leaks": leaks, "unsupported_numbers": unsupported,
            "repeated_lines": repeated, "repeated_ngram_loops": loops}


def load_manifest(path: Path) -> list[dict]:
    payload = json.loads(path.read_text())
    if payload.get("version") != 1 or not isinstance(payload.get("cases"), list) or not payload["cases"]:
        raise ValueError("manifest requires version 1 and non-empty cases")
    cases, seen = [], set()
    for row in payload["cases"]:
        identity = str(row["id"])
        if identity in seen:
            raise ValueError("case ids must be unique")
        seen.add(identity)
        source = row.get("source")
        if source not in {"owned", "synthetic", "public"}:
            raise ValueError(f"case {identity} source must be owned, synthetic or public")
        transcript_path = path.parent / row["transcript_file"]
        segments = parse_transcript(transcript_path.read_text(), transcript_path.suffix.lower())
        notes = row.get("notes")
        if not isinstance(notes, dict) or not notes:
            raise ValueError(f"case {identity} needs notes: {{label: file}}")
        key = row["key"]
        if isinstance(key, str):
            key = json.loads((path.parent / key).read_text())
        if not key.get("items"):
            raise ValueError(f"case {identity} key needs items")
        for item in key["items"] + key.get("contradictions", []):
            _groups(item)
        cases.append({"id": identity, "source": source, "segments": segments, "key": key,
                      "notes": {str(label): (path.parent / file) for label, file in notes.items()},
                      "inputs": {transcript_path.resolve()} | {(path.parent / f).resolve() for f in notes.values()}
                      | ({(path.parent / row["key"]).resolve()} if isinstance(row["key"], str) else set())})
    return cases


def evaluate(manifest: Path, gates: dict | None = None) -> dict:
    gates = DEFAULT_GATES | (gates or {})
    cases = []
    for case in load_manifest(manifest):
        candidates = {}
        for label, note_path in case["notes"].items():
            try:
                candidates[label] = score_note(note_path.read_text(), case["segments"], case["key"], gates)
            except (OSError, ValueError) as exc:
                candidates[label] = {"verdict": "error", "error": str(exc)}
        cases.append({"id": case["id"], "source": case["source"], "candidates": candidates})
    verdicts = [c["verdict"] for case in cases for c in case["candidates"].values()]
    return {"schema": SCHEMA, "gates": gates, "cases": cases,
            "summary": {verdict: verdicts.count(verdict) for verdict in ("pass", "review", "fail", "error")},
            "policy": {"matching": "lexical; NFKC, casefold, whitespace removed; any listed term group",
                       "weights": WEIGHTS,
                       "limits": "misses can be paraphrases; scores measure agreement with the human key only"}}


def protected_inputs(manifest: Path) -> set[Path]:
    paths = {manifest.resolve()}
    for case in load_manifest(manifest):
        paths |= case["inputs"]
    return paths
