"""Central gateway text provider: aliases, provenance, reuse and subscription reserve."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import httpx
import openai
import pytest

from localplaud.config import AiGatewayLlmConfig, Settings
from localplaud.llm import ai_gateway
from localplaud.llm.ai_gateway import AiGatewayLLM, alias_policy
from localplaud.llm.base import (
    LLMQuotaExhausted,
    LLMTimeout,
    LLMTransientError,
    LLMUnavailable,
    build_llm,
    capture_resolved_models,
)
from localplaud.llm.codex_quota import CodexQuotaReader
from localplaud.providers.contracts import Capability, StageCapabilities
from localplaud.providers.resolver import resolve_profile
from localplaud.worker.pipeline import (
    _profile_stage_matches,
    _settings_for_stage,
    _validate_remote_returned_model,
)

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _config(**updates) -> AiGatewayLlmConfig:
    return AiGatewayLlmConfig(
        api_key="client-key", base_url="http://gateway.test/v1", **updates
    )


def _fake_openai(monkeypatch, events=None, error=None):
    calls: list[dict] = []
    clients: list[dict] = []

    class Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            if error is not None:
                raise error
            return iter(events or [])

    def make_client(**kwargs):
        clients.append(kwargs)
        return SimpleNamespace(responses=Responses())

    # Keep the real exception classes so the adapter can classify failures.
    module = SimpleNamespace(**{name: getattr(openai, name) for name in dir(openai)})
    module.OpenAI = make_client
    monkeypatch.setitem(sys.modules, "openai", module)
    return calls, clients


def _quota(monkeypatch, remaining):
    reads: list[CodexQuotaReader] = []

    def read(self):
        reads.append(self)
        return remaining

    monkeypatch.setattr(CodexQuotaReader, "_remaining_quota_percent", read)
    return reads


def _completed(model):
    return SimpleNamespace(type="response.completed", response=SimpleNamespace(model=model))


def test_gateway_requests_alias_streams_without_sdk_retries_and_records_answering_model(
    monkeypatch,
):
    calls, clients = _fake_openai(
        monkeypatch,
        [
            SimpleNamespace(type="response.created"),
            SimpleNamespace(type="response.output_text.delta", delta='{"ok":true}'),
            _completed("upstream-model-a"),
        ],
    )
    reads = _quota(monkeypatch, 60)
    provider = build_llm(
        Settings(llm={"provider": "ai-gateway", "ai_gateway": _config().model_dump()}).llm
    )
    assert isinstance(provider, AiGatewayLLM)

    with capture_resolved_models() as resolved:
        assert provider.complete("text", system="rules", json_schema=SCHEMA) == '{"ok":true}'

    assert resolved == {"sky-quality": ["upstream-model-a"]}
    request = calls[0]
    assert request["model"] == "sky-quality"
    assert request["stream"] is True
    assert request["reasoning"] == {"effort": "high"}
    assert request["text"]["format"]["strict"] is True
    assert clients[0]["max_retries"] == 0
    assert clients[0]["timeout"] == 1800.0
    # The reserve is read from the Codex login that shares the subscription.
    assert reads[0].cfg.codex_home == "~/.codex"
    assert provider.polish_chunk_chars == 8_000
    assert provider.summary_chunk_chars == 32_000


def test_gateway_reserve_blocks_before_any_request(monkeypatch):
    calls, _clients = _fake_openai(monkeypatch, [_completed("m")])
    _quota(monkeypatch, 7)
    provider = AiGatewayLLM(_config())

    with pytest.raises(LLMQuotaExhausted, match="reserve protected"):
        provider.complete("text")
    assert calls == []
    healthy, detail = provider.health()
    assert not healthy and "reserve protected" in detail


def test_quota_login_must_report_the_configured_account():
    reader = AiGatewayLLM(_config(quota_account_id="account-a"))._quota_reader()
    reader._check_quota_account({"accountId": "account-a"})
    with pytest.raises(LLMUnavailable, match="subscription account"):
        reader._check_quota_account({"accountId": "account-b"})


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (openai.APITimeoutError(request=httpx.Request("POST", "http://g")), LLMTimeout),
        (httpx.ReadTimeout("stalled"), LLMTimeout),
        (openai.APIError("stream broke", request=httpx.Request("POST", "http://g"), body=None),
         LLMTransientError),
        (
            openai.RateLimitError(
                "limit",
                response=httpx.Response(429, request=httpx.Request("POST", "http://g")),
                body=None,
            ),
            LLMQuotaExhausted,
        ),
    ],
)
def test_gateway_failures_use_the_stage_error_contract(monkeypatch, error, expected):
    _fake_openai(monkeypatch, error=error)
    _quota(monkeypatch, 60)
    with pytest.raises(expected):
        AiGatewayLLM(_config()).complete("text")


def test_gateway_stream_has_a_whole_call_limit(monkeypatch):
    _fake_openai(
        monkeypatch,
        [SimpleNamespace(type="response.output_text.delta", delta="x"), _completed("m")],
    )
    _quota(monkeypatch, 60)
    clock = iter([0.0, 31.0, 62.0])
    monkeypatch.setattr("localplaud.llm.openai_llm.time.monotonic", lambda: next(clock))
    with pytest.raises(LLMTimeout):
        AiGatewayLLM(_config(timeout_seconds=30)).complete("text")


def test_profile_projection_uses_alias_and_secret_reference(monkeypatch):
    monkeypatch.setenv("TEST_GATEWAY_KEY", "client-key")
    snapshot = {
        "stages": {
            "summarize": {
                "connection": "llm:ai-gateway",
                "provider_type": "ai-gateway",
                "model": "sky-quality",
                "secret_ref": "env:TEST_GATEWAY_KEY",
                "configuration": {"base_url": "http://gateway.test/v1", "timeout_seconds": 900},
                "options": {},
            }
        }
    }
    resolved = _settings_for_stage(Settings(), snapshot, "summarize").llm
    assert resolved.provider == "ai-gateway"
    assert resolved.ai_gateway.model == "sky-quality"
    assert resolved.ai_gateway.api_key == "client-key"
    assert resolved.ai_gateway.timeout_seconds == 900
    with pytest.raises(ValueError, match="supports only"):
        _settings_for_stage(Settings(), {"stages": {"embed": snapshot["stages"]["summarize"]}}, "embed")


def _resolve(target, revision="r1"):
    catalog = {
        ("llm:ai-gateway", "sky-quality"): Capability(
            execution_target="cloud",
            data_egress=True,
            stages=(StageCapabilities(stage="summarize"),),
        )
    }
    details = {
        "llm:ai-gateway": {
            "provider_type": "ai-gateway",
            "execution_target": "cloud",
            "data_egress": True,
            "configuration": {"base_url": "http://gateway.test/v1"},
            "secret_ref": "env:K",
            "gateway_policy": {"revision": revision, "aliases": {"sky-quality": target}},
        }
    }
    layer = {"stages": {"summarize": {"connection": "llm:ai-gateway", "model": "sky-quality"}}}
    return resolve_profile([layer], catalog, details).to_dict()


def test_alias_remap_is_not_treated_as_the_same_selection():
    before = _resolve("model-a")
    assert before["stages"]["summarize"]["alias_resolution"] == {
        "revision": "r1",
        "model": "model-a",
    }
    assert "gateway_policy" not in before["stages"]["summarize"]
    # A policy revision that leaves this alias alone keeps artifacts reusable.
    assert _profile_stage_matches(before, _resolve("model-a", revision="r2"), "summarize")
    assert not _profile_stage_matches(before, _resolve("model-b", revision="r2"), "summarize")


def test_alias_policy_reads_revision_and_tolerates_missing_file(tmp_path):
    path = tmp_path / "model-policy.json"
    path.write_text(json.dumps({"revision": "r1", "aliases": {"sky-fast": "m"}}))
    assert alias_policy(str(path)) == {"revision": "r1", "aliases": {"sky-fast": "m"}}
    assert alias_policy(str(tmp_path / "missing.json")) == {"revision": None, "aliases": {}}
    assert alias_policy(None) == {"revision": None, "aliases": {}}
    ai_gateway._POLICY_CACHE.clear()


def test_unknown_alias_target_never_vouches_for_reuse():
    unknown = _resolve(None)
    assert unknown["stages"]["summarize"]["alias_resolution"]["model"] is None
    assert not _profile_stage_matches(unknown, _resolve(None), "summarize")
    assert not _profile_stage_matches(unknown, _resolve("model-a"), "summarize")


def test_remote_workers_still_attest_the_exact_requested_model():
    snapshot = {"stages": {"summarize": {"model": "sky-quality"}}}
    with pytest.raises(ValueError, match="different model"):
        _validate_remote_returned_model(
            {"model": "model-a", "requested_model": "sky-quality"}, snapshot, "summarize"
        )


def test_reserve_uses_the_tighter_reported_window():
    result = {"rateLimits": {"primary": {"usedPercent": 40}, "secondary": {"usedPercent": 95}}}
    assert CodexQuotaReader._remaining_from_rate_limit_result(result) == 5
    result["rateLimits"]["secondary"] = None
    assert CodexQuotaReader._remaining_from_rate_limit_result(result) == 60
