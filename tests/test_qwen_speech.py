from pathlib import Path
from types import SimpleNamespace

import pytest

from localplaud.asr.base import AsrError, Segment, Transcript, Word
from localplaud.asr.qwen_provider import QwenProvider
from localplaud.asr.speech_runtime import aligned_words, speech_regions
from localplaud.config import AsrConfig, DiarizeConfig
from localplaud.worker.diarize import diarize


def test_alignment_preserves_point_words_and_original_timeline():
    words = aligned_words(
        [
            {"text": "你好", "start_time": 0, "end_time": 0},
            {"text": "world", "start_time": 1, "end_time": 2.5},
        ],
        100,
        2,
    )
    assert words == [
        {"text": "你好", "start": 100, "end": 100},
        {"text": "world", "start": 101, "end": 102},
    ]


@pytest.mark.parametrize("start,end", [(float("nan"), 1), (2, 1), (-1, 1)])
def test_invalid_alignment_fails(start, end):
    with pytest.raises(AsrError):
        aligned_words([{"text": "a", "start_time": start, "end_time": end}], 0, 10)


def test_vad_skips_silence_and_bounds_chunks(monkeypatch):
    import sys

    monkeypatch.setitem(
        sys.modules, "faster_whisper.audio", SimpleNamespace(decode_audio=lambda *a, **k: [])
    )
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper.vad",
        SimpleNamespace(
            VadOptions=lambda **kw: kw,
            get_speech_timestamps=lambda *a: [
                {"start": 160000, "end": 320000},
                {"start": 1600000, "end": 8000000},
            ],
        ),
    )
    cfg = AsrConfig(provider="qwen").model_dump()
    spans = speech_regions(Path("unused"), cfg, 450)
    assert spans[0][0] > 9
    assert spans[1][0] > 99
    assert spans[-1][1] == 450
    assert all(e - s <= 120 + 1e-8 for s, e in spans)


def test_qwen_retains_metadata_and_words(monkeypatch):
    from localplaud.asr import qwen_provider

    monkeypatch.setattr(
        qwen_provider,
        "run_speech_process",
        lambda *args: {
            "segments": [
                {
                    "text": "你好",
                    "start": 10,
                    "end": 11,
                    "words": [{"text": "你好", "start": 10, "end": 10}],
                }
            ],
            "model": "Qwen/Qwen3-ASR-1.7B-hf",
            "provider": "qwen",
            "processing_metadata": {"vad": {"skipped_seconds": 10}},
        },
    )
    transcript = QwenProvider(AsrConfig(provider="qwen")).transcribe(Path("raw.wav"))
    assert transcript.segments[0].words[0].end == 10
    assert transcript.processing_metadata["vad"]["skipped_seconds"] == 10


def test_nemotron_keeps_global_speakers_and_point_words(monkeypatch):
    from localplaud.asr import qwen_provider

    monkeypatch.setattr(
        qwen_provider,
        "run_speech_process",
        lambda *a: {
            "turns": [(1, 2, "speaker_0"), (100, 101, "speaker_1"), (200, 201, "speaker_0")]
        },
    )
    transcript = Transcript(
        segments=[
            Segment(
                "a b c", 1, 201, words=[Word("a", 1, 1), Word("b", 100, 101), Word("c", 200, 201)]
            )
        ]
    )
    result = diarize(Path("raw.wav"), transcript, DiarizeConfig(provider="nemotron"))
    assert [w.speaker for w in result.segments[0].words] == ["speaker_0", "speaker_1", "speaker_0"]
    assert result.has_speakers


def test_explicit_backfill_includes_overlong_recordings():
    from datetime import UTC, datetime

    from localplaud.config import Settings
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.worker.pipeline import _pending_scope

    row = PlaudFile(
        id="long",
        status=FileStatus.downloaded,
        audio_path="raw.wav",
        duration_ms=6 * 3600 * 1000,
        process_overlong=False,
    )
    assert _pending_scope(row, Settings(), datetime.now(UTC)) is None
    row.process_overlong = True
    assert _pending_scope(row, Settings(), datetime.now(UTC)) == "full"


def test_activation_preserves_completed_audio_and_existing_transcripts(monkeypatch, tmp_path):
    import importlib.util

    import localplaud.db.session as db
    from localplaud.config import get_settings
    from localplaud.db.models import (
        FileStatus,
        ModelCatalogEntry,
        PlaudFile,
        ProviderConnection,
        RecordingProfileOverride,
        RemoteWorker,
    )
    from localplaud.db.models import (
        Transcript as StoredTranscript,
    )
    from localplaud.providers.contracts import Capability, StageCapabilities
    from localplaud.providers.service import list_profiles

    spec = importlib.util.spec_from_file_location(
        "activate", Path(__file__).parents[1] / "scripts/maintenance/activate_qwen_nemotron.py"
    )
    rollout = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rollout)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path}/rollout.db")
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_Session", None)
    get_settings(reload=True)
    db.init_db()
    caps = [
        {"stage": "transcribe", "models": [rollout.ASR]},
        {"stage": "diarize", "models": [rollout.DIARIZE]},
    ]
    monkeypatch.setattr(rollout, "check_worker", lambda *a: {"status": "healthy"})
    with db.session_scope() as session:
        from sqlalchemy import select

        from localplaud.db.models import ExecutionProfile

        profile = session.scalar(select(ExecutionProfile).where(ExecutionProfile.is_system_default))
        profile.no_egress = False
        profile.privacy_policy = "allow-egress"
        session.add(
            RemoteWorker(key="gpu", name="gpu", base_url="http://worker", capabilities=caps)
        )
        conn = ProviderConnection(
            key="worker:gpu",
            name="gpu",
            provider_type="localplaud-worker",
            execution_target="remote_worker",
            data_egress=True,
            config={},
        )
        session.add(conn)
        session.flush()
        for stage, model in [("transcribe", rollout.ASR), ("diarize", rollout.DIARIZE)]:
            session.add(
                ModelCatalogEntry(
                    connection_id=conn.id,
                    model_key=model,
                    display_name=model,
                    capabilities=Capability(
                        execution_target="remote_worker",
                        data_egress=True,
                        stages=(StageCapabilities(stage=stage),),
                    ).model_dump(mode="json"),
                )
            )
        session.add_all(
            [
                PlaudFile(id="done", status=FileStatus.done, is_trash=False, audio_path="done.wav"),
                PlaudFile(id="new", status=FileStatus.metadata_only, is_trash=False),
                PlaudFile(
                    id="partial", status=FileStatus.partial, is_trash=False, audio_path="old.wav"
                ),
            ]
        )
        session.flush()
        session.add(
            StoredTranscript(
                file_id="partial",
                source="local",
                text="user text",
                segments=[],
                provider="faster-whisper",
                model="large-v3-turbo",
            )
        )
    assert rollout.activate("gpu")["unfinished"] == 2
    result = rollout.activate("gpu", apply=True)
    assert result["queued"] == 2
    with db.session_scope() as session:
        assert session.get(PlaudFile, "done").status == FileStatus.done
        assert session.get(RecordingProfileOverride, "done") is None
        assert session.get(PlaudFile, "partial").local_transcript.text == "user text"
        assert session.get(PlaudFile, "partial").status == FileStatus.partial
        assert session.get(PlaudFile, "new").status == FileStatus.discovered
        assert (
            session.get(RecordingProfileOverride, "new").stage_overrides["transcribe"]["model"]
            == rollout.ASR
        )
        assert "align" not in session.get(RecordingProfileOverride, "partial").stage_overrides
        default = next(p for p in list_profiles(session) if p["is_system_default"])
        assert default["stages"]["transcribe"]["model"] == rollout.ASR


@pytest.mark.parametrize(
    "model,expected", [("Qwen/Qwen3-ASR-1.7B-hf", "qwen"), ("large-v3-turbo", "faster-whisper")]
)
def test_remote_speech_dispatch_preserves_queued_model_and_metadata(monkeypatch, model, expected):
    import base64
    import json

    from localplaud.config import Settings
    from localplaud.remote import server
    from localplaud.remote.protocol import JobSubmitRequest
    from localplaud.worker import transcribe

    settings = Settings()
    settings.asr.provider = "qwen"
    monkeypatch.setattr(server, "get_settings", lambda: settings)

    def run(path, selected):
        assert selected.asr.provider == expected
        return Transcript(
            segments=[],
            provider=expected,
            model=model,
            processing_metadata={"vad": {"skipped_seconds": 30}},
        )

    monkeypatch.setattr(transcribe, "run_asr", run)
    request = JobSubmitRequest(
        idempotency_key="speech-dispatch",
        stage="transcribe",
        model=model,
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
    payload = json.loads(base64.b64decode(artifacts[0]["data_base64"]))
    assert payload["model"] == model
    assert payload["processing_metadata"]["vad"]["skipped_seconds"] == 30


def test_token_limit_splits_only_affected_region_and_keeps_full_timeline():
    from localplaud.asr.speech_runtime import TokenLimitError, transcribe_bounded

    calls = []

    def decode(start, end):
        calls.append((start, end))
        if start == 100 and end == 120:
            raise TokenLimitError("limit")
        return f"{start}-{end}"

    result, splits = transcribe_bounded([(10, 20), (100, 120), (200, 210)], decode)
    assert [(s, e) for s, e, _ in result] == [(10, 20), (100, 110), (110, 120), (200, 210)]
    assert calls.count((10, 20)) == 1
    assert splits == [(100, 120)]


def test_irreducible_token_limit_never_returns_partial_success():
    from localplaud.asr.speech_runtime import TokenLimitError, transcribe_bounded

    def decode(start, end):
        raise TokenLimitError("limit")

    with pytest.raises(AsrError, match="transcription is incomplete"):
        transcribe_bounded([(10, 12)], decode)


def test_checkpoint_reuses_exact_audio_configuration_and_survives_partial_write(tmp_path):
    from localplaud.asr.speech_checkpoint import SpeechCheckpoint

    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"first audio")
    cfg = AsrConfig(provider="qwen").model_dump(mode="json")
    cfg["qwen"]["checkpoint_dir"] = str(tmp_path / "checkpoints")
    cache = SpeechCheckpoint(audio, cfg)
    cache.write(10, 20, {"transcription": "你好", "language": "Chinese"})
    cache.write(20, 40, {"token_limited": True})
    resumed = SpeechCheckpoint(audio, cfg)
    assert resumed.read(10, 20)["transcription"] == "你好"
    assert resumed.read(20, 40)["token_limited"] is True
    resumed._path(10, 20).with_suffix(".tmp").write_text("incomplete")
    assert resumed.read(10, 20)["transcription"] == "你好"
    cfg["qwen"]["revision"] = "another revision"
    assert SpeechCheckpoint(audio, cfg).read(10, 20) is None
    cfg["qwen"]["revision"] = AsrConfig().qwen.revision
    audio.write_bytes(b"other audio")
    assert SpeechCheckpoint(audio, cfg).read(10, 20) is None
