"""Conservative, deterministic cross-recording voice matching (no network or ML)."""

from __future__ import annotations

import math
import re
from collections import defaultdict

MODEL = "nvidia/speakerverification_en_titanet_large"
REVISION = "0dc382f40121a5fbd34db10a2bb04d826c2be6a8"
VERSION = "voice-identity/v1"


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

    def match(self, query_vectors, *, threshold=0.75, margin=0.12, min_votes=2, query_file_id=None):
        import numpy as np

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
        }
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


def match_voice(query_vectors, references, **kwargs):
    return VoiceMatcher(references).match(query_vectors, **kwargs)
