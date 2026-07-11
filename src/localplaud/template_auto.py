"""Deterministic, local-only note-template recommendation."""

from __future__ import annotations

from collections import defaultdict

_SIGNALS = {
    "meeting": (
        "meeting", "standup", "sync", "agenda", "minutes", "action item",
        "decision", "會議", "開會", "議程", "決議", "待辦", "同步",
    ),
    "call": (
        "phone call", "video call", "customer call", "sales call", "interview",
        "電話", "通話", "訪談", "客戶",
    ),
    "lecture": (
        "lecture", "class", "course", "professor", "lesson", "seminar",
        "演講", "課程", "上課", "教授", "講座", "研討會",
    ),
    "personal": (
        "voice memo", "memo to self", "note to self", "remember to", "my idea",
        "語音備忘", "提醒自己", "我的想法", "靈感", "日記",
    ),
}


def recommend_template(
    *, title: str = "", transcript: str = "", duration_ms: int | None = None
) -> dict:
    """Return a stable recommendation and human-readable evidence.

    No network or model is involved; identical inputs always produce the same result.
    """
    title_text = title.casefold()
    transcript_text = transcript.casefold()
    scores: dict[str, int] = defaultdict(int, {"default": 1})
    evidence: dict[str, list[str]] = defaultdict(list)
    for key, phrases in _SIGNALS.items():
        for phrase in phrases:
            title_hits = title_text.count(phrase)
            body_hits = min(3, transcript_text.count(phrase))
            if title_hits:
                scores[key] += 4 * title_hits
                evidence[key].append(f'title contains “{phrase}”')
            if body_hits:
                scores[key] += body_hits
                evidence[key].append(f'transcript mentions “{phrase}”')
    if duration_ms and duration_ms >= 45 * 60 * 1000:
        scores["lecture"] += 1
        evidence["lecture"].append("recording is longer than 45 minutes")
    ranked = sorted(scores, key=lambda key: (-scores[key], key != "default", key))
    selected = ranked[0]
    confidence = "high" if scores[selected] >= 7 else "medium" if scores[selected] >= 4 else "low"
    reasons = evidence[selected][:3]
    if not reasons:
        reasons = ["no strong scenario signal; use the balanced default"]
    return {
        "key": selected,
        "confidence": confidence,
        "reasons": reasons,
        "scores": {key: scores.get(key, 0) for key in ("default", "meeting", "call", "lecture", "personal")},
        "engine": "local-deterministic-v1",
    }
