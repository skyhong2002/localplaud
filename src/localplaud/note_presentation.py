"""Keep recording playback citations out of readable note prose."""

import re

_PLAYBACK = re.compile(
    r"[ \t]*\[(?:\d+:)?\d{2}:\d{2}\]"
    r"\(/file/[A-Za-z0-9_-]+\?t=\d+(?:\.\d+)?\)"
)


def without_playback_citations(markdown: str) -> str:
    """Remove generated links, preserving stated times and unrelated links."""
    return _PLAYBACK.sub("", markdown)
