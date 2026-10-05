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

    import torch

    from localplaud.asr.speech_runtime import offload_long_audio_features

    calls = []

    class Preprocessor:
        featurizer = SimpleNamespace(hop_length=160, n_fft=512)

        def to(self, device):
            calls.append(("preprocessor", device))

        def __call__(self, input_signal, length):
            calls.append(("features", int(input_signal.shape[-1]), input_signal.device.type))
            frames = int(length[0]) // 160 + 1
            return torch.zeros(1, 4, frames + 3), torch.tensor([frames])

    original = object()
    model = SimpleNamespace(
        streaming_mode=True,
        device="cpu",
        preprocessor=Preprocessor(),
        process_signal=original,
        speaker_cache=object(),
    )
    cache = model.speaker_cache
    offload_long_audio_features(model, 60)
    assert model.process_signal is original
    offload_long_audio_features(model, 8635)
    features, lengths = model.process_signal(
        torch.zeros(1, 160 * 130_000), torch.tensor([160 * 130_000])
    )
    # The preprocessor moved to CPU and the recording was never featurized in one piece.
    assert calls[0] == ("preprocessor", "cpu")
    slab_sizes = [call[1] for call in calls[1:]]
    assert len(slab_sizes) >= 3 and max(slab_sizes) < 160 * 130_000
    assert all(call[2] == "cpu" for call in calls[1:])
    assert int(lengths[0]) == features.shape[-1] == 130_001
    assert model.speaker_cache is cache


def test_memory_error_is_actionable_without_exposing_runtime_context():
    from localplaud.asr.speech_runtime import runtime_error

    message = runtime_error(
        "nemotron", RuntimeError("CUDA driver error: out of memory private/path")
    )
    assert "out of memory" in message
    assert "retry this stage" in message
    assert "private/path" not in message


class _FramingPreprocessor:
    """Stand-in with the real transform's framing: centred reflect padding, a
    preemphasis sample, 512-sample windows at hop 160, and output padded to a
    multiple of 16 frames."""

    def __init__(self):
        from types import SimpleNamespace

        import torch

        self.featurizer = SimpleNamespace(hop_length=160, n_fft=512)
        generator = torch.Generator().manual_seed(7)
        self.window = torch.hann_window(512)
        self.mix = torch.rand(257, 12, generator=generator)
        self.calls = []

    def __call__(self, input_signal, length):
        import torch
        from torch.nn import functional

        self.calls.append(int(input_signal.shape[-1]))
        x = torch.cat([input_signal[:, :1], input_signal[:, 1:] - 0.97 * input_signal[:, :-1]], 1)
        x = functional.pad(x.unsqueeze(1), (256, 256), mode="reflect").squeeze(1)
        frames = x.unfold(1, 512, 160) * self.window
        mel = (torch.fft.rfft(frames).abs() ** 2 @ self.mix).clamp_min(1e-9).log()
        mel = mel.transpose(1, 2)
        pad = (-mel.shape[-1]) % 16
        return functional.pad(mel, (0, pad)), length // 160 + 1


def test_slab_features_equal_the_whole_signal_features_exactly():
    import torch

    from localplaud.asr.speech_runtime import chunked_features

    generator = torch.Generator().manual_seed(11)
    for samples in (160 * 4000 + 77, 160 * 1234, 160 * 999 + 159, 160 * 321 + 1):
        signal = torch.randn(1, samples, generator=generator)
        length = torch.tensor([samples])
        reference = _FramingPreprocessor()
        whole, whole_length = reference(signal, length)
        sliced = _FramingPreprocessor()
        slabs, slab_length = chunked_features(sliced, signal, length, chunk_frames=100)

        assert int(slab_length[0]) == int(whole_length[0])
        frames = int(whole_length[0])
        assert slabs.shape[-1] == frames
        assert torch.allclose(slabs, whole[:, :, :frames], atol=1e-5, rtol=1e-5), samples
        # The recording really was cut into pieces, none anywhere near its size.
        if samples > 160 * 1000:
            assert len(sliced.calls) > 5 and max(sliced.calls) < samples // 4


def test_slab_features_keep_short_and_batched_input_as_one_call():
    import torch

    from localplaud.asr.speech_runtime import chunked_features

    pre = _FramingPreprocessor()
    short = torch.randn(1, 160 * 50)
    chunked_features(pre, short, torch.tensor([160 * 50]), chunk_frames=100)
    assert pre.calls == [160 * 50]
    pre = _FramingPreprocessor()
    batch = torch.randn(2, 160 * 5000)
    chunked_features(pre, batch, torch.tensor([160 * 5000] * 2), chunk_frames=100)
    assert pre.calls == [160 * 5000]
