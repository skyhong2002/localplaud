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


def fold_script(text: str) -> str:
    """Fold Simplified characters to Traditional for script-insensitive matching.

    Mixed-script ASR output (for example 「对，等等這樣子」) is legitimately quoted
    by models in one script. This is a comparison key only and never replaces
    stored text. Returns the input unchanged when OpenCC is not installed.
    """
    global _detect, _unavailable
    if not text or _unavailable:
        return text
    if _detect is None:
        try:
            from opencc import OpenCC

            _detect = OpenCC("s2t")
        except Exception as exc:  # noqa: BLE001 - matching falls back to exact text
            log.warning("OpenCC unavailable; leaving Chinese text unconverted: %s", exc)
            _unavailable = True
            return text
    try:
        return _detect.convert(text)
    except Exception:  # noqa: BLE001
        return text


_KANA = re.compile(r"[\u3040-\u30ff]")
# OpenCC's longest-match segmentation reads 「情系统」 as 「情系」+「统」 and
# yields 「情繫統」. 「繫統」 is not a Traditional word, so restore 「系統」.
_MISSEGMENTED = (("繫統", "系統"),)


def _repair(text: str) -> str:
    for wrong, right in _MISSEGMENTED:
        text = text.replace(wrong, right)
    return text


_taiwan_chars = None


def to_taiwan_script(text: str) -> str:
    """Convert Simplified characters to Taiwan Traditional characters only.

    Unlike :func:`to_traditional`, this uses OpenCC's character-level ``s2tw``
    table without phrase rewriting, so already-Taiwan wording (權限, 軟體) and
    mixed English stay untouched. Text containing Japanese kana is left alone.
    Returns the input unchanged when OpenCC is not installed.
    """
    global _taiwan_chars, _unavailable
    if not text or _unavailable or _KANA.search(text):
        return text
    if _taiwan_chars is None:
        try:
            from opencc import OpenCC

            _taiwan_chars = OpenCC("s2tw")
        except Exception as exc:  # noqa: BLE001 - normalisation must never break a stage
            log.warning("OpenCC unavailable; leaving Chinese text unconverted: %s", exc)
            _unavailable = True
            return text
    try:
        return _repair(_taiwan_chars.convert(text))
    except Exception:  # noqa: BLE001
        return text


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

            _detect = _detect or OpenCC("s2t")
            _convert = OpenCC("s2twp")
        except Exception as exc:  # noqa: BLE001 - normalisation must never break a stage
            log.warning("OpenCC unavailable; leaving Chinese text unconverted: %s", exc)
            _unavailable = True
            return text
    try:
        if _detect.convert(text) == text:
            return text  # already Traditional (or non-Chinese)
        protected = _NEIGHBOURHOOD_PREFIX.sub(_NEIGHBOURHOOD_TOKEN, text)
        return _repair(_convert.convert(protected).replace(_NEIGHBOURHOOD_TOKEN, "社區"))
    except Exception:  # noqa: BLE001
        return text
