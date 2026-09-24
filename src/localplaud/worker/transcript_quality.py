"""Conservative, read-only diagnostics for a provider's raw ASR transcript.

This module reports evidence for the pipeline to persist; it does not repair or
replace the provider result. In particular, little speech is not evidence that
the recording is empty or that ASR failed.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from localplaud.asr.base import Transcript

_LEXICAL = re.compile(r"[^\W\d_]+", re.UNICODE)
_FILLER = frozenset({"thank", "you", "yeah", "okay", "ok", "thanks"})
_ERROR_MESSAGE = "逐字稿看起來有異常；請檢查音訊、語言設定與 ASR 設定後重試。"


class TranscriptQualityError(RuntimeError):
    """A blocking assessment, with the complete JSON-safe report attached."""

    def __init__(self, assessment: dict):
        super().__init__(_ERROR_MESSAGE)
        self.assessment = assessment


def _valid_time(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_span(start: object, end: object) -> bool:
    return _valid_time(start) and _valid_time(end) and 0 <= start <= end


def assess_transcript(transcript: Transcript) -> dict:
    """Return a versioned, JSON-safe quality assessment without changing input."""

    issues: list[dict[str, str]] = []

    def issue(code: str, severity: str, message: str) -> None:
        issues.append({"code": code, "severity": severity, "message": message})

    duration = transcript.duration
    if duration is not None and (not _valid_time(duration) or duration < 0):
        issue("invalid_duration", "error", "錄音長度無效，請檢查音訊與時間資料後重試。")

    segments = transcript.segments
    nonempty = [segment for segment in segments if segment.text.strip()]
    invalid_segments = sum(not _valid_span(s.start, s.end) for s in segments)
    invalid_words = sum(
        not _valid_span(word.start, word.end) for segment in segments for word in segment.words
    )
    if invalid_segments or invalid_words:
        issue("invalid_timestamps", "error", "逐字稿時間戳無效，請檢查音訊與 ASR 結果後重試。")

    # Use the furthest valid segment endpoint when the provider omitted duration.
    valid_ends = [float(s.end) for s in segments if _valid_span(s.start, s.end)]
    measured_duration = (
        float(duration)
        if duration is not None and _valid_time(duration) and duration >= 0
        else None
    )
    effective_duration = (
        measured_duration if measured_duration is not None else max(valid_ends, default=0.0)
    )

    tokens_by_segment = [
        [token.casefold() for token in _LEXICAL.findall(segment.text)] for segment in nonempty
    ]
    token_count = sum(len(tokens) for tokens in tokens_by_segment)
    filler_count = sum(token in _FILLER for tokens in tokens_by_segment for token in tokens)
    substantive_count = token_count - filler_count
    filler_ratio = filler_count / token_count if token_count else 0.0
    filler_only_segments = sum(
        bool(tokens) and all(token in _FILLER for token in tokens) for tokens in tokens_by_segment
    )

    # A substantial non-filler utterance is strong counterevidence even if a
    # recording contains a large amount of filler-like speech elsewhere.
    substantive_utterance = any(
        any(token not in _FILLER for token in tokens) and len(segment.text.strip()) >= 20
        for segment, tokens in zip(nonempty, tokens_by_segment, strict=True)
    )
    fixed_filler_loop = (
        effective_duration >= 180
        and len(nonempty) >= 20
        and token_count >= 20
        and filler_ratio >= 0.90
        and filler_only_segments / len(nonempty) >= 0.90
        and substantive_count <= 2
        and not substantive_utterance
    )
    if fixed_filler_loop:
        issue(
            "fixed_filler_loop",
            "error",
            "逐字稿疑似重複固定語句；請檢查音訊、語言與 ASR 設定後重試。",
        )

    if effective_duration >= 180 and token_count < 20:
        issue("sparse_text", "warning", "長錄音只有少量辨識文字；請視需要核對音訊與逐字稿。")

    valid_nonempty = sorted(
        (s for s in nonempty if _valid_span(s.start, s.end)), key=lambda s: s.start
    )
    max_gap = max(
        (
            float(right.start - left.end)
            for left, right in zip(valid_nonempty, valid_nonempty[1:], strict=False)
        ),
        default=0.0,
    )
    max_gap = max(0.0, max_gap)
    if max_gap >= 120:
        issue("long_gap", "warning", "逐字稿中有較長空白時段；請視需要核對音訊。")

    normalized = [" ".join(s.text.casefold().split()) for s in nonempty]
    repeated_segments = sum(count for count in Counter(normalized).values() if count >= 3)
    repeated_ratio = repeated_segments / len(nonempty) if nonempty else 0.0
    if len(nonempty) >= 5 and repeated_ratio >= 0.60 and not fixed_filler_loop:
        issue("repeated_phrases", "warning", "逐字稿有多次相同語句；請視需要核對音訊。")

    missing_speaker_count = sum(not s.speaker for s in nonempty)
    if missing_speaker_count:
        issue("missing_speaker_labels", "warning", "部分逐字稿尚無講者標籤；請檢查講者辨識狀態。")

    return {
        "version": "transcript-quality/v1",
        "status": "degraded" if issues else "passed",
        "blocking": any(item["severity"] == "error" for item in issues),
        "issues": issues,
        "metrics": {
            "duration_seconds": measured_duration,
            "effective_duration_seconds": effective_duration,
            "segment_count": len(segments),
            "nonempty_segment_count": len(nonempty),
            "lexical_token_count": token_count,
            "filler_token_count": filler_count,
            "substantive_token_count": substantive_count,
            "filler_token_ratio": filler_ratio,
            "filler_only_segment_count": filler_only_segments,
            "invalid_segment_timestamp_count": invalid_segments,
            "invalid_word_timestamp_count": invalid_words,
            "max_gap_seconds": max_gap,
            "repeated_segment_ratio": repeated_ratio,
            "missing_speaker_segment_count": missing_speaker_count,
        },
    }


def require_usable_transcript(transcript: Transcript) -> dict:
    """Return the assessment or raise with it if the transcript is unusable."""

    assessment = assess_transcript(transcript)
    if assessment["blocking"]:
        raise TranscriptQualityError(assessment)
    return assessment
