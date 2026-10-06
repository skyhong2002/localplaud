"""Conservative, deterministic cross-recording voice matching (no network or ML)."""

from __future__ import annotations

import math
import re
from collections import defaultdict

MODEL = "nvidia/speakerverification_en_titanet_large"
REVISION = "0dc382f40121a5fbd34db10a2bb04d826c2be6a8"
VERSION = "voice-identity/v1"


# Calibrated confidence --------------------------------------------------------
#
# Raw similarity is not confidence: on this library a speaker who is NOT enrolled has
# a best-match similarity around 0.60 (90% of them reach 0.69), so any absolute cutoff
# in the usual 0.4-0.5 range accepts almost everyone. What separates a known person
# from a stranger is how far the best name leads the runner-up and how many independent
# recordings of that name agree. A logistic model over those three signals turns them
# into the probability that the best name is right.
#
# Fitted on 2026-10-07 from the 406 enrolled voiceprints (26 names) of this library,
# each scored against every other recording of the same name (known) and with its name
# withheld entirely (stranger). Grouped 5-fold cross-validation: at p>=0.70 it names 71%
# of known speakers at 98.3% precision and wrongly names 2.5% of strangers; at p>=0.50
# 81% / 97.9% / 11.3%. Refit with scripts/maintenance/fit_voice_calibration.py.
CALIBRATION = {
    "version": "voice-calibration/v1",
    "features": ["score", "margin", "log1p_files"],
    "mean": [0.6491, 0.1172, 3.0195],
    "scale": [0.1088, 0.0760, 1.5327],
    "bias": -0.209,
    "weights": [2.335, 0.082, 0.671],
}
# Guards the model never learned from: a best match below this similarity is never
# named, and the best name must clearly lead the runner-up. Two names that are nearly
# equally close (an alias pair, similar voices) are a coin flip however high the
# similarity, and on this library the lead guard costs 0.5 points of coverage and no
# precision at p>=0.70.
SCORE_FLOOR = 0.5
MIN_LEAD = 0.04
POLICIES = ("strict", "calibrated")


def calibrated_probability(score, runner_up, files, calibration=None) -> float:
    """Probability that the best-matching name is correct (see ``CALIBRATION``)."""
    model = calibration or CALIBRATION
    values = [float(score), float(score) - float(runner_up or 0.0), math.log1p(max(0, int(files)))]
    if not all(math.isfinite(v) for v in values):
        return 0.0
    z = model["bias"] + sum(
        w * (v - m) / s
        for w, v, m, s in zip(model["weights"], values, model["mean"], model["scale"], strict=True)
    )
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))


def usable_name(name: str) -> bool:
    name = str(name or "").strip()
    return bool(
        name
        and len(name) <= 128
        and not re.search(r"[/／、&＆+]|\b(?:and|or)\b", name, re.I)
        and not re.fullmatch(
            r"(?:speaker|speeker|語者|说话人|說話人|講者|unknown|person|spk)[ _#-]*\d*|\d+[ #]*|[?#-]+",
            name,
            re.I,
        )
    )


def cosine(a: list[float], b: list[float]) -> float:
    if not a or len(a) != len(b) or not all(math.isfinite(x) for x in [*a, *b]):
        raise ValueError("invalid voice vector")
    norm = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    if norm == 0:
        raise ValueError("empty voice vector")
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b, strict=True)) / norm))


def clean_windows(
    segments: list[dict], max_per_speaker=6, min_seconds=3.0, max_seconds=10.0
) -> dict:
    """Keep separated, non-overlapping speech, with a boundary guard on both ends."""
    if max_per_speaker < 1 or min_seconds <= 0 or max_seconds < min_seconds:
        raise ValueError("invalid voice window limits")
    intervals = defaultdict(list)
    for seg in segments or []:
        words = [w for w in seg.get("words", []) if w.get("speaker")]
        # Word stamps often have tiny gaps. Merge only within the same speaker.
        for x in words or [seg]:
            try:
                a, b = float(x["start"]), float(x["end"])
            except (KeyError, ValueError, TypeError):
                continue
            key = x.get("speaker")
            if key and math.isfinite(a) and math.isfinite(b) and 0 <= a < b:
                intervals[str(key)].append((a, b))
    merged = {}
    for key, values in intervals.items():
        out = []
        for a, b in sorted(values):
            if out and a <= out[-1][1] + 0.2:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        merged[key] = out
    result = {}
    for key, values in merged.items():
        candidates = []
        other = sorted(x for k, v in merged.items() if k != key for x in v)
        for a, b in values:
            pieces = [(a, b)]
            for c, d in other:
                if d <= a or c >= b:
                    continue
                pieces = [
                    (x, y)
                    for left, right in pieces
                    for x, y in [(left, min(right, c)), (max(left, d), right)]
                    if y > x
                ]
            for left, right in pieces:
                left, right = left + 0.15, right - 0.15
                while right - left >= min_seconds:
                    end = min(right, left + max_seconds)
                    candidates.append([round(left, 3), round(end, 3)])
                    left = end
        if candidates:
            # Spread samples over the whole recording; never duplicate a window.
            n = min(max_per_speaker, len(candidates))
            indexes = (
                [round(i * (len(candidates) - 1) / (n - 1)) for i in range(n)] if n > 1 else [0]
            )
            result[key] = [candidates[i] for i in indexes]
    return result


class VoiceMatcher:
    """Prepared matrix of reference centroids; each file supplies at most one vote."""

    def __init__(self, references):
        import numpy as np

        self.refs = [r for r in references if usable_name(r["name"]) and r.get("vectors")]
        self.groups = defaultdict(lambda: defaultdict(list))
        centroids = []
        dimension = None
        for index, ref in enumerate(self.refs):
            vectors = np.asarray(ref["vectors"], dtype=float)
            if vectors.ndim != 2 or not np.isfinite(vectors).all():
                raise ValueError("invalid voice reference")
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if np.any(norms == 0):
                raise ValueError("empty voice reference")
            centroid = (vectors / norms).mean(axis=0)
            norm = np.linalg.norm(centroid)
            if norm == 0 or (dimension is not None and len(centroid) != dimension):
                raise ValueError("incompatible voice reference")
            dimension = len(centroid)
            centroids.append(centroid / norm)
            self.groups[ref["name"]][ref["file_id"]].append(index)
        self.matrix = np.asarray(centroids)

    def match(
        self,
        query_vectors,
        *,
        threshold=0.75,
        margin=0.12,
        min_votes=2,
        query_file_id=None,
        policy="strict",
        min_probability=0.7,
    ):
        import numpy as np

        if policy not in POLICIES:
            raise ValueError(f"unknown voice matching policy: {policy}")

        empty = {
            "status": "insufficient",
            "name": None,
            "score": 0.0,
            "runner_up": 0.0,
            "reference_ids": [],
        }
        if len(query_vectors) < min_votes or not self.refs:
            return empty
        query = np.asarray(query_vectors, dtype=float)
        if (
            query.ndim != 2
            or query.shape[1] != self.matrix.shape[1]
            or not np.isfinite(query).all()
        ):
            raise ValueError("invalid query voice vector")
        norms = np.linalg.norm(query, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise ValueError("empty query voice vector")
        similarity = (query / norms) @ self.matrix.T
        ranked = []
        for name, files in self.groups.items():
            indexes = [indices for fid, indices in files.items() if fid != query_file_id]
            if not indexes:
                continue
            per_file = np.stack([similarity[:, indices].max(axis=1) for indices in indexes], axis=1)
            ordered = np.sort(per_file, axis=1)
            scores = ordered[:, -2] if len(indexes) >= 2 else ordered[:, -1]
            score = float(np.median(scores))
            ranked.append((score, name, scores, len(indexes)))
        if not ranked:
            return empty
        ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
        score, name, scores, files = ranked[0]
        second = ranked[1][0] if len(ranked) > 1 else 0.0
        votes = int(np.sum(scores >= threshold))
        supported = [
            i
            for fid, indexes in self.groups[name].items()
            if fid != query_file_id
            for i in indexes
            if int(np.sum(similarity[:, i] >= threshold)) >= min_votes
        ]
        result = {
            "status": "unknown",
            "name": name,
            "score": score,
            "runner_up": second,
            "reference_ids": [
                [self.refs[i]["file_id"], self.refs[i]["speaker_key"]] for i in supported
            ],
            "reference_sample_ids": [self.refs[i].get("sample_id") for i in supported],
            "votes": votes,
            "windows": len(query_vectors),
            "files": files,
        }
        if policy == "calibrated":
            probability = calibrated_probability(score, second, files)
            result["probability"] = round(probability, 4)
            result["policy"] = "calibrated"
            result["calibration"] = CALIBRATION["version"]
            result["min_probability"] = min_probability
            if (
                score >= SCORE_FLOOR
                and probability >= min_probability
                and score - second >= MIN_LEAD
            ):
                result["status"] = "matched"
            elif score >= SCORE_FLOOR:
                result["status"] = "ambiguous"
            return result
        if score < threshold:
            return result
        result["status"] = "ambiguous"
        if (
            files >= 2
            and score - second >= margin
            and votes >= min_votes
            and votes / len(scores) >= 2 / 3
            and len({self.refs[i]["file_id"] for i in supported}) >= 2
        ):
            result["status"] = "matched"
        return result


def resolve_name_conflicts(decisions: dict, taken: dict | None = None) -> None:
    """Make one recording's speakers hold distinct names (mutates ``decisions``).

    Two speakers in one recording are different people, so two cannot share a name.
    ``decisions`` maps speaker key to a matcher decision; ``taken`` maps speaker key to
    the name that speaker already holds (a person's choice or an earlier automatic
    name). Of several matched speakers claiming one name only the most probable keeps
    it; a name already held by a different speaker is unavailable to everyone else.
    Losers become ``ambiguous`` and keep their candidate so it can still be suggested.
    """
    taken = taken or {}
    claims = defaultdict(list)
    for key, decision in decisions.items():
        if decision.get("status") == "matched":
            claims[decision["name"]].append(key)
    for name, keys in claims.items():
        holders = {k for k, held in taken.items() if held == name}
        if holders:
            winner = (
                next(iter(holders)) if len(holders) == 1 and next(iter(holders)) in keys else None
            )
        else:
            winner = max(
                keys,
                key=lambda k: (decisions[k].get("probability", decisions[k]["score"]), k),
            )
        for key in keys:
            if key != winner:
                decisions[key]["status"] = "ambiguous"
                decisions[key]["demoted"] = "name_taken_in_recording"


def match_voice(query_vectors, references, **kwargs):
    return VoiceMatcher(references).match(query_vectors, **kwargs)
