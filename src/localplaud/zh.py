"""Traditional-Chinese (Taiwan) normalisation for generated text.

Whisper's Mandarin transcripts come out in Simplified script and local LLMs
follow suit, but this library's user-facing language is Taiwan Traditional.
Conversion runs controller-side on short generated strings (titles, tags);
if OpenCC is unavailable (e.g. inside the GPU worker image) text passes
through unchanged rather than failing the pipeline.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_detect = None
_convert = None
_unavailable = False


def to_traditional(text: str | None) -> str | None:
    """Convert Simplified Chinese to Traditional (Taiwan phrasing).

    Text that contains no Simplified characters is returned UNCHANGED — the
    s2twp phrase dictionary would otherwise rewrite already-correct Taiwan
    wording (權限→許可權, 設備→裝置) and damage proper nouns. Detection uses
    the s2t fixed-point: only strings that s2t actually changes are Simplified.
    Returns the input unchanged when OpenCC is not installed.
    """
    global _detect, _convert, _unavailable
    if not text or _unavailable:
        return text
    if _convert is None:
        try:
            from opencc import OpenCC

            _detect = OpenCC("s2t")
            _convert = OpenCC("s2twp")
        except Exception as exc:  # noqa: BLE001 - normalisation must never break a stage
            log.warning("OpenCC unavailable; leaving Chinese text unconverted: %s", exc)
            _unavailable = True
            return text
    try:
        if _detect.convert(text) == text:
            return text  # already Traditional (or non-Chinese)
        return _convert.convert(text)
    except Exception:  # noqa: BLE001
        return text
