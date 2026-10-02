"""The Codex subscription-window read that guards the AI gateway's reserve."""

from __future__ import annotations

import pytest

from localplaud.config import CodexQuotaConfig
from localplaud.llm import codex_quota
from localplaud.llm.base import LLMQuotaExhausted, LLMUnavailable
from localplaud.llm.codex_quota import CodexQuotaReader


def _reader(monkeypatch, remaining):
    reader = CodexQuotaReader(CodexQuotaConfig())
    monkeypatch.setattr(reader, "_remaining_quota_percent", lambda: remaining)
    return reader


def test_quota_reserve_stops_before_protected_floor(monkeypatch):
    with pytest.raises(LLMQuotaExhausted, match="reserve protected.*7%"):
        _reader(monkeypatch, 7)._ensure_quota_reserve()


def test_quota_reserve_allows_safe_headroom(monkeypatch):
    assert _reader(monkeypatch, 8)._ensure_quota_reserve() == 8


def test_quota_snapshot_prefers_main_codex_bucket():
    assert (
        CodexQuotaReader._remaining_from_rate_limit_result(
            {
                "rateLimits": {"primary": {"usedPercent": 99}},
                "rateLimitsByLimitId": {
                    "codex": {"primary": {"usedPercent": 41}},
                    "codex_bengalfox": {"primary": {"usedPercent": 0}},
                },
            }
        )
        == 59
    )
    with pytest.raises(LLMUnavailable, match="incomplete"):
        CodexQuotaReader._remaining_from_rate_limit_result(
            {"rateLimits": {"primary": {"usedPercent": True}}}
        )


def test_quota_check_timeout_is_read_once_more_then_fails_closed(monkeypatch):
    reader = CodexQuotaReader(CodexQuotaConfig())
    outcomes = [codex_quota._QuotaCheckTimeout("timed out"), 60]

    def read():
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(reader, "_remaining_quota_percent", read)
    assert reader._ensure_quota_reserve() == 60

    calls = []

    def always_slow():
        calls.append(1)
        raise codex_quota._QuotaCheckTimeout("timed out")

    monkeypatch.setattr(reader, "_remaining_quota_percent", always_slow)
    with pytest.raises(LLMUnavailable, match="timed out"):
        reader._ensure_quota_reserve()
    assert len(calls) == 2


def test_quota_reader_never_runs_a_model_turn():
    assert not hasattr(CodexQuotaReader, "complete")
