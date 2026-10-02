"""Synthetic speech metrics and offline harness contracts; no model downloads."""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from localplaud import benchmark_speech as speech
from localplaud.asr.base import Segment, Transcript, Word
from localplaud.config import Settings


def test_mixed_tokenization_without_spaces_and_normalization():
    assert speech.mixed_tokens("臺灣OpenAI測試 don't １２３！") == ["臺", "灣", "openai", "測", "試", "don't", "123"]
    assert speech.characters("Ａ b。臺灣") == list("ab臺灣")
    assert speech.text_metrics("臺灣 test", "臺灣 TEST!")["cer"]["rate"] == 0
    assert speech.text_metrics("臺灣", "台湾")["cer"]["rate"] == 1


@pytest.mark.parametrize("ref,hyp,counts", [
    ([], [], (0, 0, 0)), (["a"], [], (0, 0, 1)), ([], ["a"], (0, 1, 0)),
    (["a", "b"], ["a", "c"], (1, 0, 0)), (["a"], ["a", "b"], (0, 1, 0)),
])
def test_edit_counts(ref, hyp, counts):
    result = speech.edit_counts(ref, hyp)
    assert (result["substitutions"], result["insertions"], result["deletions"]) == counts
    assert result["errors"] == sum(counts)
    if not ref and hyp:
        assert result["rate"] is None  # Undefined denominator, never a fake perfect score.


def test_mixed_mer_uses_chinese_characters_and_english_words():
    result = speech.text_metrics("我們用 hello world", "我們用 hello earth")
    assert result["mer"]["reference_units"] == 5
    assert result["mer"]["rate"] == 0.2
    assert result["wer"] == result["mer"]


def turn(start, end, speaker):
    return {"start": start, "end": end, "speaker": speaker}


def test_der_permuted_speakers_exact_and_overlap():
    reference = [turn(0, 2, "A"), turn(1, 3, "B")]
    hypothesis = [turn(0, 2, "2"), turn(1, 3, "1")]
    result = speech.diarization_error(reference, hypothesis)
    assert result["rate"] == 0
    assert result["reference_speaker_seconds"] == 4
    assert result["mapping"] == {"2": "A", "1": "B"}


def test_der_miss_false_alarm_confusion_and_empty_denominator():
    result = speech.diarization_error([turn(0, 2, "A"), turn(2, 4, "B")], [turn(1, 5, "X")])
    assert result["missed_seconds"] == 1
    assert result["false_alarm_seconds"] == 1
    assert result["confusion_seconds"] == 1
    assert result["rate"] == 0.75
    assert speech.diarization_error([], [turn(0, 1, "X")])["rate"] is None
    assert speech.diarization_error([], [])["rate"] == 0


def test_assignment_finds_global_not_greedy_optimum():
    assert speech._assignment([[9, 8], [8, 0]]) == {0: 1, 1: 0}


def test_rttm_selection_and_validation():
    data = "SPEAKER one 1 0.5 1.5 <NA> <NA> A <NA>\nSPEAKER two 1 1 1 <NA> <NA> B <NA>"
    assert speech.parse_rttm(data, "one") == [turn(0.5, 2, "A")]
    with pytest.raises(ValueError, match="multi-recording"):
        speech.parse_rttm(data)
    with pytest.raises(ValueError):
        speech.parse_rttm("SPEAKER one 1 nan 2 <NA> <NA> A <NA>")


def test_hallucination_loop_and_non_speech_annotations():
    segments = [{"text": "hello world hello world hello world", "start": 0, "end": 3}]
    result = speech.hallucination_indicators(segments, [{"start": 1, "end": 2}, {"start": 1.5, "end": 2.5}])
    assert any(loop["ngram_size"] == 2 and loop["repeats"] == 3 for loop in result["repeated_ngram_loops"])
    assert result["text_in_non_speech"][0]["overlap_seconds"] == 1.5
    assert speech.hallucination_indicators(segments, None)["text_in_non_speech"] is None
    assert speech.hallucination_indicators(segments, [])["text_in_non_speech"] == []


def test_timestamp_lexical_matching_survives_insertions():
    ref = [{"text": "hello", "start": 0, "end": 1}, {"text": "世界", "start": 1, "end": 2}]
    hyp = [{"text": "extra", "start": 0, "end": 0.1}, {"text": "hello", "start": 0.2, "end": 1.2}, {"text": "世界", "start": 1.2, "end": 2.2}]
    result = speech.timestamp_deviation(ref, hyp)
    assert result["matched_units"] == 3
    assert result["mean_absolute_seconds"] == pytest.approx(0.2)
    assert speech.timestamp_deviation(ref, [])["mean_absolute_seconds"] is None


def manifest(tmp_path):
    audio = tmp_path / "owned.wav"
    audio.write_bytes(b"synthetic")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"version": 1, "recordings": [
        {"id": "owned", "owned": True, "audio": audio.name,
         "reference": {"text": "臺灣 hello", "non_speech": [], "speaker_turns": [turn(0, 2, "A")]}}
    ]}))
    return path


def fake_executor(audio, settings, alignment):
    assert isinstance(audio, Path)
    return Transcript(segments=[Segment(text="臺灣 hello", start=0, end=2, speaker="X", words=[
        Word(text="臺灣 hello", start=0, end=2)
    ])], provider="fake", model="turbo", has_speakers=True), 2, {
        "elapsed_seconds": 1, "peak_memory_bytes": 1024, "stages": {"diarization_complete": True}}


def test_manifest_harness_and_duration_weighted_aggregation(tmp_path):
    path = manifest(tmp_path)
    settings, align, provenance = speech.configuration(Settings(_env_file=None))
    report = speech.benchmark(path, settings, align, provenance, executor=fake_executor)
    assert report["recordings"][0]["metrics"]["der"]["rate"] == 0  # Word inherits segment speaker.
    assert report["aggregate"]["cer"]["rate"] == 0
    assert report["aggregate"]["real_time_factor"] == 0.5
    assert report["aggregate"]["peak_memory_bytes"] == 1024
    worse = report["recordings"][0] | {"metrics": speech.score_recording({"text": "長一點的參考"}, Transcript(segments=[]))}
    total = speech.aggregate(report["recordings"] + [worse])
    assert total["cer"]["rate"] == pytest.approx(6 / 13)
    assert total["der"]["recordings"] == 1


@pytest.mark.parametrize("provider", ["openai", "deepgram", "assemblyai"])
def test_no_upload_rejects_cloud_before_manifest_or_execution(provider, tmp_path):
    settings = Settings(_env_file=None, asr={"provider": provider})
    with pytest.raises(ValueError, match="never uploads"):
        speech.benchmark(tmp_path / "absent.json", settings, {"provider": "whisperx"}, {})
    with pytest.raises(ValueError):
        speech.configuration(settings)


def test_configuration_preserves_stage_options_and_rejects_cloud_fallback():
    settings, alignment, provenance = speech.configuration(Settings(_env_file=None), explicit={
        "asr": {"provider": "faster-whisper", "faster_whisper": {"model": "turbo", "device": "cpu"}},
        "vad": {"enabled": True}, "align": {"provider": "whisperx", "model": "wav2vec2-auto"},
        "diarize": {"provider": "none"}})
    assert settings.asr.vad.enabled and settings.asr.faster_whisper.model == "turbo"
    assert not settings.pipeline.diarize and alignment["provider"] == "whisperx"
    assert "hf_token" not in provenance["diarization_options"]
    with pytest.raises(ValueError, match="never uploads"):
        speech.configuration(Settings(_env_file=None, asr={"fallback": ["openai"]}))


def test_failed_recording_is_reported_not_removed(tmp_path):
    settings, align, provenance = speech.configuration(Settings(_env_file=None))
    def fail(*args):
        raise RuntimeError("local model unavailable")
    report = speech.benchmark(manifest(tmp_path), settings, align, provenance, executor=fail)
    assert report["aggregate"]["failed"] == 1
    assert report["recordings"][0]["error"] == "local model unavailable"
    assert report["aggregate"]["peak_memory_bytes"] is None


def test_manifest_requires_ownership_and_does_not_modify_inputs(tmp_path):
    path = manifest(tmp_path)
    data = json.loads(path.read_text())
    data["recordings"][0]["owned"] = False
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="owned"):
        speech.load_manifest(path)
    assert (tmp_path / "owned.wav").read_bytes() == b"synthetic"


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_json_table_and_saved_report(monkeypatch, tmp_path, json_mode):
    from localplaud import cli

    path = manifest(tmp_path)
    settings, align, provenance = speech.configuration(Settings(_env_file=None))
    report = speech.benchmark(path, settings, align, provenance, executor=fake_executor)
    monkeypatch.setattr(speech, "benchmark", lambda *a, **kw: report)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    destination = tmp_path / "report.json"
    args = ["benchmark-speech", str(path), "--output", str(destination)]
    if json_mode:
        args.append("--json")
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(destination.read_text())["aggregate"]["completed"] == 1
    if json_mode:
        assert json.loads(result.stdout)["schema"] == "localplaud-speech-benchmark/v1"
    else:
        assert "Local speech benchmark" in result.stdout and "Aggregate" in result.stdout
    blocked = CliRunner().invoke(cli.app, ["benchmark-speech", str(path), "--output", str(tmp_path / "owned.wav")])
    assert blocked.exit_code != 0 and (tmp_path / "owned.wav").read_bytes() == b"synthetic"


def test_local_execution_order_and_temporary_outputs(monkeypatch, tmp_path):
    import wave
    from types import SimpleNamespace

    from localplaud.worker import align, convert, diarize, transcribe

    order = []
    def convert_audio(audio, destination):
        order.append("convert")
        with wave.open(str(destination), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0\0" * 16000)
        return destination
    def asr(audio, settings):
        order.append("asr")
        return Transcript(segments=[Segment(text="hi", start=0, end=1)], provider="fake")
    def alignment(audio, transcript, **kwargs):
        order.append("align")
        return SimpleNamespace(transcript=transcript, provider="fake", model="model", detail={})
    def speakers(audio, transcript, config):
        order.append("diarize")
        transcript.has_speakers = True
        return transcript
    monkeypatch.setattr(convert, "to_wav", convert_audio)
    monkeypatch.setattr(transcribe, "run_asr", asr)
    monkeypatch.setattr(align, "run_alignment", alignment)
    monkeypatch.setattr(diarize, "diarize", speakers)
    settings, selection, _ = speech.configuration(Settings(_env_file=None))
    result, duration, execution = speech._execute_local(tmp_path / "owned.wav", settings, selection)
    assert order == ["convert", "asr", "align", "diarize"]
    assert result.has_speakers and duration == 1 and execution["elapsed_seconds"] >= 0


def test_profile_is_read_only_and_remote_selection_rejected(monkeypatch, tmp_path):
    from sqlalchemy import select

    import localplaud.config as config
    import localplaud.db.session as db
    from localplaud.db.models import ExecutionProfile, ProviderConnection
    from localplaud.db.session import session_scope

    settings = Settings(_env_file=None, store={"database_url": f"sqlite:///{tmp_path / 'profile.db'}"})
    monkeypatch.setattr(config, "_settings", settings)
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_Session", None)
    db.init_db()
    with session_scope() as session:
        profile = session.scalar(select(ExecutionProfile).where(ExecutionProfile.is_system_default))
        identity, version = profile.id, profile.version
    _, _, provenance = speech.configuration(settings, profile_id=identity)
    assert provenance["profile_id"] == identity
    with session_scope() as session:
        assert session.get(ExecutionProfile, identity).version == version
        connection = session.get(ProviderConnection, next(
            s.connection_id for s in session.get(ExecutionProfile, identity).stage_selections if s.stage == "transcribe"
        ))
        connection.execution_target = "remote_worker"
        connection.data_egress = True
    with pytest.raises(ValueError, match="non-local transcribe|no-egress"):
        speech.configuration(settings, profile_id=identity)
    with pytest.raises(ValueError, match="OR explicit"):
        speech.configuration(settings, profile_id=identity, explicit={"vad": {"enabled": True}})


def test_tree_memory_includes_descendants(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(speech.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="10 1 100\n11 10 200\n12 11 300\n99 1 900\n"))
    assert speech._tree_rss(10) == 600 * 1024
