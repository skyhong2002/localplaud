from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from localplaud.config import Settings
from localplaud.db.models import StageName
from localplaud.providers.failures import (
    ProviderUnavailableError,
    RetryableProviderError,
    allows_provider_fallback,
    is_retryable_sdk_failure,
)
from localplaud.worker import pipeline

STAGE_ROUTES = [
    (StageName.transcribe, "faster-whisper", "openai"),
    (StageName.align, "whisper-timestamps", "whisperx"),
    (StageName.diarize, "pyannote", "remote-worker"),
    (StageName.summarize, "ollama", "openai"),
    (StageName.index, "local", "openai"),
]


def _install_snapshot(monkeypatch, primary, fallback):
    run = SimpleNamespace(profile_snapshot={
        "route": {"provider": primary, "fallback": [{"provider": fallback}]},
    })

    @contextmanager
    def fake_session_scope():
        yield SimpleNamespace(scalar=lambda statement: run)

    monkeypatch.setattr(pipeline, "session_scope", fake_session_scope)


def _routed_provider(settings, stage):
    if stage is StageName.transcribe:
        return settings.asr.provider
    if stage is StageName.align:
        return settings.align.provider
    if stage is StageName.diarize:
        return settings.diarize.provider
    if stage is StageName.index:
        return settings.embeddings.provider
    return settings.llm.provider


@pytest.mark.parametrize("failure", [
    ValueError("invalid configuration"),
    AssertionError("bug"),
    PermissionError("forbidden"),
    RuntimeError("hard provider failure"),
])
def test_unclassified_failures_are_not_fallback_eligible(failure):
    assert allows_provider_fallback(failure) is False


def test_auth_http_failure_is_not_fallback_eligible():
    request = httpx.Request("POST", "https://provider.example/v1/jobs")
    response = httpx.Response(401, request=request)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    assert allows_provider_fallback(raised.value) is False


@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
def test_retryable_sdk_http_statuses_are_classified(status):
    request = httpx.Request("POST", "https://provider.example/v1")
    response = httpx.Response(status, request=request)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    assert is_retryable_sdk_failure(raised.value) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_nonretryable_sdk_http_statuses_are_not_classified(status):
    request = httpx.Request("POST", "https://provider.example/v1")
    response = httpx.Response(status, request=request)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    assert is_retryable_sdk_failure(raised.value) is False


def test_sdk_transport_timeout_is_retryable():
    assert is_retryable_sdk_failure(httpx.ReadTimeout("timed out")) is True


@pytest.mark.parametrize("failure", [
    ProviderUnavailableError("offline"),
    RetryableProviderError("overloaded"),
])
def test_deliberately_classified_failures_are_fallback_eligible(failure):
    assert allows_provider_fallback(failure) is True


def test_classification_survives_provider_adapter_wrapping():
    try:
        raise RetryableProviderError("transient")
    except RetryableProviderError as cause:
        wrapper = RuntimeError("adapter failed")
        wrapper.__cause__ = cause
    assert allows_provider_fallback(wrapper) is True


def test_implicit_exception_context_does_not_classify_programming_bug():
    try:
        raise RetryableProviderError("transient")
    except RetryableProviderError:
        try:
            raise AssertionError("bug while handling provider failure")
        except AssertionError as bug:
            assert allows_provider_fallback(bug) is False


@pytest.mark.parametrize(("stage", "primary", "fallback"), STAGE_ROUTES)
@pytest.mark.parametrize("failure", [
    ValueError("invalid input"),
    AssertionError("programming bug"),
    PermissionError("authentication failed"),
    RuntimeError("non-retryable provider failure"),
])
def test_pipeline_boundary_does_not_fallback_unclassified_failures(
    monkeypatch, stage, primary, fallback, failure,
):
    _install_snapshot(monkeypatch, primary, fallback)
    calls = []

    def operation(routed):
        calls.append(_routed_provider(routed, stage))
        raise failure

    with pytest.raises(type(failure), match=str(failure)):
        pipeline._run_with_fallback("file", stage, Settings(), operation)
    assert calls == [primary]


@pytest.mark.parametrize(("stage", "primary", "fallback"), STAGE_ROUTES)
@pytest.mark.parametrize("failure", [
    ProviderUnavailableError("unavailable"),
    RetryableProviderError("retryable"),
])
def test_pipeline_boundary_uses_explicit_fallback_for_classified_failures(
    monkeypatch, stage, primary, fallback, failure,
):
    _install_snapshot(monkeypatch, primary, fallback)
    calls = []

    def operation(routed):
        provider = _routed_provider(routed, stage)
        calls.append(provider)
        if provider == primary:
            raise failure
        return provider

    result, _, route, fallback_index = pipeline._run_with_fallback(
        "file", stage, Settings(), operation,
    )
    assert result == fallback
    assert route["provider"] == fallback
    assert fallback_index == 1
    assert calls == [primary, fallback]
