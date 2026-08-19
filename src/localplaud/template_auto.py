"""Plaud Autopilot selection for recordings left in Automatic mode."""

from __future__ import annotations


def recommend_template(
    *, title: str = "", transcript: str = "", duration_ms: int | None = None
) -> dict:
    """Return the user's captured Plaud Autopilot template without inventing one."""
    del title, transcript, duration_ms
    return {
        "key": "plaud-autopilot",
        "confidence": "high",
        "reasons": ["Automatic mode uses the captured Plaud Autopilot template"],
        "scores": {"plaud-autopilot": 1},
        "engine": "plaud-recent-template-v1",
    }
