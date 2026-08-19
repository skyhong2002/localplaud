"""Traditional-Chinese (Taiwan) normalisation for generated text.

Whisper's Mandarin transcripts come out in Simplified script and local LLMs
follow suit, but this library's user-facing language is Taiwan Traditional.
Conversion runs controller-side on short generated strings (titles, tags);
if OpenCC is unavailable (e.g. inside the GPU worker image) text passes
through unchanged rather than failing the pipeline.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_detect = None
_convert = None
_unavailable = False

# OpenCC's s2twp phrase dictionary correctly renders an online "community" as
# 「社群」, but applies the same rewrite to residential compounds such as
# 「社區管委會」.  Protect only the neighbourhood-specific prefix while the
# remainder of the sentence is converted; broad replacement of 「社区」 would
# damage legitimate phrases such as 「線上社群」.
_NEIGHBOURHOOD_PREFIX = re.compile(
    r"社[区區](?=(?:管委[会會]|規約|规约|住戶|住户|住民|大樓|大楼|物業|物业|治理|管理|事務|事务))"
)
_NEIGHBOURHOOD_TOKEN = "__LOCALPLAUD_NEIGHBOURHOOD__"


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
        protected = _NEIGHBOURHOOD_PREFIX.sub(_NEIGHBOURHOOD_TOKEN, text)
        return _convert.convert(protected).replace(_NEIGHBOURHOOD_TOKEN, "社區")
    except Exception:  # noqa: BLE001
        return text
