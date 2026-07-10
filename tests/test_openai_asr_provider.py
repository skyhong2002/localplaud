import sys
import wave
from types import SimpleNamespace

from localplaud.config import AsrConfig


def _wav(path):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\0\0" * 16_000)


def _provider(monkeypatch, model, response, seen):
    class Transcriptions:
        def create(self, **kwargs):
            seen.update(kwargs)
            return response

    class Client:
        def __init__(self, **kwargs):
            seen["client"] = kwargs
            self.audio = SimpleNamespace(transcriptions=Transcriptions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=Client))
    from localplaud.asr.openai_provider import OpenAIProvider

    cfg = AsrConfig(provider="openai")
    cfg.openai.api_key = "test-key"
    cfg.openai.model = model
    return OpenAIProvider(cfg)


def test_gpt4o_transcribe_uses_json_and_degrades_without_fake_timestamps(
    monkeypatch, tmp_path
):
    path = tmp_path / "audio.wav"
    _wav(path)
    seen = {}
    provider = _provider(
        monkeypatch, "gpt-4o-mini-transcribe",
        SimpleNamespace(text="hello", language="en"), seen,
    )
    transcript = provider.transcribe(path)
    assert seen["response_format"] == "json"
    assert "timestamp_granularities" not in seen
    assert transcript.text == "hello"
    assert transcript.duration == 1.0
    assert transcript.segments[0].start == 0
    assert transcript.segments[0].end == 1.0
    assert transcript.segments[0].words == []


def test_whisper_and_diarize_models_use_their_supported_response_shapes(
    monkeypatch, tmp_path
):
    path = tmp_path / "audio.wav"
    _wav(path)
    whisper_seen = {}
    whisper = _provider(
        monkeypatch, "whisper-1",
        SimpleNamespace(
            language="en", duration=1.0,
            segments=[SimpleNamespace(text="hello", start=0.0, end=1.0)],
            words=[SimpleNamespace(word="hello", start=0.1, end=0.9)],
        ), whisper_seen,
    ).transcribe(path)
    assert whisper_seen["response_format"] == "verbose_json"
    assert whisper_seen["timestamp_granularities"] == ["segment", "word"]
    assert whisper.segments[0].words[0].text == "hello"

    diarize_seen = {}
    diarized = _provider(
        monkeypatch, "gpt-4o-transcribe-diarize",
        SimpleNamespace(
            language="en", duration=1.0,
            segments=[SimpleNamespace(
                text="hello", start=0.0, end=1.0, speaker="speaker_0"
            )],
        ), diarize_seen,
    ).transcribe(path)
    assert diarize_seen["response_format"] == "diarized_json"
    assert diarize_seen["chunking_strategy"] == "auto"
    assert diarized.has_speakers is True
