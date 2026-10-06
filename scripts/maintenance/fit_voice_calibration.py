"""Refit the voice-confidence calibration from this library's enrolled voiceprints.

Read-only: prints a cross-validated report and the constants for
``localplaud.voice_matching.CALIBRATION``; it never changes a name or the database.

Each enrolled voiceprint is scored two ways against the profile's frozen reference set:
as a *known* speaker (every other recording of that name stays in the references) and as
a *stranger* (all of that name withheld, so any acceptance is wrong). A logistic model
over similarity, lead over the runner-up and number of independent recordings is fitted,
and a grouped 5-fold cross-validation (a recording never trains and tests together)
reports coverage, precision and stranger false-accept rate by probability threshold.

    python scripts/maintenance/fit_voice_calibration.py --profile data/voice-identity/profile.json
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import random
from pathlib import Path

import numpy as np

from localplaud.db.session import session_scope
from localplaud.voice_identity import VoiceSample, references


def _prepare(profile):
    with session_scope() as session:
        samples = list(session.query(VoiceSample).filter(VoiceSample.status == "ready"))
        refs = references(
            samples,
            plaud_enrollment=profile.get("plaud_enrollment"),
            name_aliases=profile.get("speaker_name_aliases", {}),
        )
    prepared = []
    for ref in refs:
        vectors = np.asarray(ref["vectors"], float)
        vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
        centroid = vectors.mean(0)
        prepared.append(
            {
                "name": ref["name"],
                "file": ref["file_id"],
                "vectors": vectors,
                "centroid": centroid / np.linalg.norm(centroid),
            }
        )
    return prepared


def _rank(query, refs, matrix, by_name, exclude_file, exclude_name=None):
    similarity = query["vectors"] @ matrix.T
    ranked = []
    for name, indexes in by_name.items():
        if name == exclude_name:
            continue
        files = collections.defaultdict(list)
        for i in indexes:
            if refs[i]["file"] != exclude_file:
                files[refs[i]["file"]].append(i)
        if not files:
            continue
        per_file = np.stack([similarity[:, ix].max(axis=1) for ix in files.values()], axis=1)
        ordered = np.sort(per_file, axis=1)
        scores = ordered[:, -2] if len(files) >= 2 else ordered[:, -1]
        ranked.append((float(np.median(scores)), name, len(files)))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return ranked


def _features(ranked):
    score, name, files = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0.0
    return name, [score, score - second, math.log1p(files)]


def _fit(x, y, l2=1e-2, iterations=60):
    mean, scale = x.mean(0), x.std(0) + 1e-9
    z = np.c_[np.ones(len(x)), (x - mean) / scale]
    beta = np.zeros(z.shape[1])
    for _ in range(iterations):
        p = 1 / (1 + np.exp(-z @ beta))
        weight = p * (1 - p) + 1e-9
        beta += np.linalg.solve(
            (z.T * weight) @ z + l2 * np.eye(len(beta)), z.T @ (y - p) - l2 * beta
        )
    return mean, scale, beta


def _predict(model, x):
    mean, scale, beta = model
    z = np.c_[np.ones(len(x)), (x - mean) / scale]
    return 1 / (1 + np.exp(-z @ beta))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    args = parser.parse_args()
    refs = _prepare(json.loads(args.profile.read_text()))
    matrix = np.stack([r["centroid"] for r in refs])
    by_name = collections.defaultdict(list)
    for i, ref in enumerate(refs):
        by_name[ref["name"]].append(i)
    rows, labels, files = [], [], []
    for ref in refs:
        known = _rank(ref, refs, matrix, by_name, ref["file"])
        if known:
            name, feats = _features(known)
            rows.append(feats), labels.append(1.0 if name == ref["name"] else 0.0)
            files.append(ref["file"])
    known_count = len(rows)
    for ref in refs:
        stranger = _rank(ref, refs, matrix, by_name, ref["file"], exclude_name=ref["name"])
        if stranger:
            rows.append(_features(stranger)[1]), labels.append(0.0), files.append(ref["file"])
    x, y = np.array(rows), np.array(labels)
    print(
        f"references {len(refs)}, names {len(by_name)}; known {known_count}, stranger {len(x) - known_count}"
    )
    model = _fit(x, y)
    order = sorted(set(files))
    random.Random(5).shuffle(order)
    fold = {f: i % 5 for i, f in enumerate(order)}
    probability = np.zeros(len(x))
    for k in range(5):
        test = [i for i, f in enumerate(files) if fold[f] == k]
        train = [i for i, f in enumerate(files) if fold[f] != k]
        probability[test] = _predict(_fit(x[train], y[train]), x[test])
    print("\n  p>=   coverage  precision  stranger-FA")
    for threshold in (0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3):
        accepted = probability[:known_count] >= threshold
        precision = y[:known_count][accepted].mean() if accepted.any() else 0.0
        false_accept = (probability[known_count:] >= threshold).mean()
        print(f"{threshold:5.0%}  {accepted.mean():8.1%}  {precision:9.1%}  {false_accept:11.1%}")
    print("\nCALIBRATION constants:")
    print(
        json.dumps(
            {
                "features": ["score", "margin", "log1p_files"],
                "mean": [round(v, 4) for v in model[0]],
                "scale": [round(v, 4) for v in model[1]],
                "bias": round(float(model[2][0]), 3),
                "weights": [round(float(v), 3) for v in model[2][1:]],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
