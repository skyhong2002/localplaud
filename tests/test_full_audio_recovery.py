"""Quiet speech recovery must retain every second and its explicit VAD choice."""

import base64
import json
from pathlib import Path

import pytest

from localplaud.asr.base import Transcript
from localplaud.asr.speech_runtime import TokenLimitError, speech_regions, transcribe_bounded
from localplaud.config import AsrConfig, Settings
from localplaud.remote.protocol import JobSubmitRequest


@pytest.mark.parametrize("duration", [0, 1.25, 120, 241.5, 8635.44])
def test_disabled_vad_covers_complete_audio_without_detector(duration):
    cfg = AsrConfig().model_dump()
    cfg["vad"]["enabled"] = False
    spans = speech_regions(Path("no-detector-or-file-needed.wav"), cfg, duration)
    assert sum(e - s for s, e in spans) == pytest.approx(duration)
    if spans:
        assert spans[0][0] == 0
        assert spans[-1][1] == duration
        assert all(e > s and e - s <= 120 for s, e in spans)
        assert all(a[1] == b[0] for a, b in zip(spans, spans[1:], strict=False))


def test_full_audio_token_retry_keeps_tail_and_original_timeline():
    cfg = AsrConfig().model_dump()
    cfg["vad"]["enabled"] = False
    spans = speech_regions(Path("unused"), cfg, 241.5)

    def decode(start, end):
        if end - start > 60:
            raise TokenLimitError("retry")
        return {"text": "quiet conversation"}

    done, splits = transcribe_bounded(spans, decode)
    assert splits
    assert sum(e - s for s, e, _ in done) == 241.5
    assert done[-1][:2] == (240, 241.5)


@pytest.mark.parametrize("enabled", [False, True])
def test_remote_vad_option_is_scoped_and_preserves_provenance(monkeypatch, enabled):
    from localplaud.remote import server
    from localplaud.worker import transcribe

    settings = Settings()
    settings.asr.vad.enabled = True
    settings.asr.qwen.model = "Qwen/Qwen3-ASR-1.7B-hf"
    monkeypatch.setattr(server, "get_settings", lambda: settings)

    def run(path, selected):
        assert selected.asr.provider == "qwen"
        assert selected.asr.vad.enabled is enabled
        return Transcript(
            segments=[],
            provider="qwen",
            model=settings.asr.qwen.model,
            processing_metadata={"vad": {"enabled": enabled}},
        )

    monkeypatch.setattr(transcribe, "run_asr", run)
    request = JobSubmitRequest(
        idempotency_key="full-audio-recovery",
        stage="transcribe",
        model=settings.asr.qwen.model,
        options={"vad": {"enabled": enabled}},
        inputs=[
            {
                "name": "audio",
                "media_type": "audio/wav",
                "kind": "inline_base64",
                "value": base64.b64encode(b"RIFF").decode(),
            }
        ],
    )
    artifacts = server._execute(request)
    result = json.loads(base64.b64decode(artifacts[0]["data_base64"]))
    assert result["processing_metadata"]["vad"]["enabled"] is enabled
    assert settings.asr.vad.enabled is True


def test_long_diarization_offloads_features_without_splitting_speaker_stream():
    from types import SimpleNamespace

    from localplaud.asr.speech_runtime import offload_long_audio_features

    calls = []

    class Tensor:
        def __init__(self, name):
            self.name = name

        def cpu(self):
            calls.append((self.name, "cpu"))
            return self

        def to(self, device):
            calls.append((self.name, device))
            return self

    class Preprocessor:
        def to(self, device):
            calls.append(("preprocessor", device))

        def __call__(self, **kwargs):
            calls.append(("whole-recording", tuple(kwargs)))
            return Tensor("features"), Tensor("lengths")

    original = object()
    model = SimpleNamespace(
        streaming_mode=True,
        device="cuda",
        preprocessor=Preprocessor(),
        process_signal=original,
        speaker_cache=object(),
    )
    cache = model.speaker_cache
    offload_long_audio_features(model, 60)
    assert model.process_signal is original
    offload_long_audio_features(model, 8635)
    model.process_signal(Tensor("audio"), Tensor("audio-length"))
    assert calls == [
        ("preprocessor", "cpu"),
        ("audio", "cpu"),
        ("audio-length", "cpu"),
        ("whole-recording", ("input_signal", "length")),
        ("features", "cuda"),
        ("lengths", "cuda"),
    ]
    assert model.speaker_cache is cache


def test_memory_error_is_actionable_without_exposing_runtime_context():
    from localplaud.asr.speech_runtime import runtime_error

    message = runtime_error(
        "nemotron", RuntimeError("CUDA driver error: out of memory private/path")
    )
    assert "out of memory" in message
    assert "retry this stage" in message
    assert "private/path" not in message
