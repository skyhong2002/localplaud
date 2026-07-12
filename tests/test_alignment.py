from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from localplaud.asr.base import Segment, Transcript, Word
from localplaud.config import Settings
from localplaud.db.models import Base, ModelCatalogEntry, ProviderConnection
from localplaud.worker.align import (
    AlignmentError,
    AlignmentUnavailable,
    inspect_word_alignment,
    run_alignment,
)


def test_provider_word_timestamps_are_validated_without_claiming_forced_alignment():
    transcript = Transcript(
        segments=[
            Segment(
                text="hello world",
                start=0,
                end=1,
                words=[
                    Word(text="hello", start=0.0, end=0.4),
                    Word(text="world", start=0.5, end=0.9),
                ],
            )
        ]
    )
    detail = inspect_word_alignment(transcript)
    assert detail == {
        "strategy": "provider-word-timestamps",
        "forced_alignment": False,
        "word_count": 2,
        "timed_segments": 1,
        "segment_count": 1,
        "segment_coverage": 1.0,
    }


@pytest.mark.parametrize(
    "words,error,match",
    [
        ([], AlignmentUnavailable, "no word timestamps"),
        ([Word(text="bad", start=1.0, end=0.5)], AlignmentError, "invalid timestamp"),
        (
            [Word(text="later", start=2, end=3), Word(text="earlier", start=1, end=2)],
            AlignmentError,
            "chronologically ordered",
        ),
    ],
)
def test_missing_or_invalid_word_timestamps_are_actionable(words, error, match):
    transcript = Transcript(segments=[Segment(text="x", start=0, end=3, words=words)])
    with pytest.raises(error, match=match):
        inspect_word_alignment(transcript)


def test_whisperx_dispatch_forces_alignment_and_preserves_asr_text(monkeypatch, tmp_path):
    import localplaud.worker.align as alignment

    calls = {}

    class FakeWhisperX:
        @staticmethod
        def load_align_model(**kwargs):
            calls["load"] = kwargs
            return object(), {"dictionary": {"你": 1}}

        @staticmethod
        def load_audio(path):
            calls["audio"] = path
            return "audio-array"

        @staticmethod
        def align(segments, _model, _metadata, audio, device, **kwargs):
            calls["align"] = {"segments": segments, "audio": audio, "device": device, **kwargs}
            return {
                "segments": [
                    {
                        "text": "changed text must not replace ASR text",
                        "start": 0.05,
                        "end": 0.95,
                        "words": [
                            {"word": "你好", "start": 0.05, "end": 0.45, "score": 0.93},
                            {"word": "world", "start": 0.5, "end": 0.95, "score": 0.88},
                        ],
                    }
                ]
            }

    monkeypatch.setattr(alignment, "_import_whisperx", lambda: FakeWhisperX)
    monkeypatch.setattr(alignment, "_resolve_device", lambda _requested: "cuda")
    monkeypatch.setattr(alignment, "_whisperx_version", lambda: "test-version")
    audio = tmp_path / "mixed.wav"
    audio.write_bytes(b"RIFF")
    transcript = Transcript(
        segments=[Segment(text="你好 world", start=0, end=1)],
        language="zh-TW",
        provider="faster-whisper",
        model="large-v3-turbo",
    )

    result = run_alignment(
        audio,
        transcript,
        provider="whisperx",
        model="wav2vec2-auto",
        options={"device": "cuda", "min_segment_coverage": 1.0},
    )

    assert result.transcript.text == "你好 world"
    assert [word.text for word in result.transcript.segments[0].words] == ["你好", "world"]
    assert result.transcript.provider == "faster-whisper"
    assert result.detail == {
        "strategy": "whisperx-wav2vec2",
        "forced_alignment": True,
        "word_count": 2,
        "timed_segments": 1,
        "segment_count": 1,
        "segment_coverage": 1.0,
        "provider": "whisperx",
        "alignment_model": "wav2vec2-auto",
        "implementation_version": "test-version",
        "device": "cuda",
        "language": "zh",
        "interpolate_method": "nearest",
        "minimum_segment_coverage": 1.0,
        "unaligned_words": 0,
    }
    assert calls["load"] == {"language_code": "zh", "device": "cuda"}
    assert calls["align"]["return_char_alignments"] is False


def test_whisperx_rejects_missing_language_and_incomplete_output(monkeypatch, tmp_path):
    import localplaud.worker.align as alignment

    class IncompleteWhisperX:
        @staticmethod
        def load_align_model(**_kwargs):
            return object(), {}

        @staticmethod
        def load_audio(_path):
            return []

        @staticmethod
        def align(*_args, **_kwargs):
            return {"segments": []}

    monkeypatch.setattr(alignment, "_import_whisperx", lambda: IncompleteWhisperX)
    monkeypatch.setattr(alignment, "_resolve_device", lambda _requested: "cpu")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    missing_language = Transcript(segments=[Segment(text="hello", start=0, end=1)])
    with pytest.raises(AlignmentUnavailable, match="transcript language"):
        run_alignment(
            audio,
            missing_language,
            provider="whisperx",
            model="wav2vec2-auto",
        )

    with pytest.raises(AlignmentError, match="different segment count"):
        run_alignment(
            audio,
            Transcript(
                segments=[Segment(text="hello", start=0, end=1)],
                language="en",
            ),
            provider="whisperx",
            model="wav2vec2-auto",
        )


def test_whisperx_catalog_model_uses_alignment_health_probe(monkeypatch, tmp_path):
    import localplaud.worker.align as alignment
    from localplaud.providers.service import bootstrap_default_profile, check_model_health

    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    Base.metadata.create_all(engine)
    calls = []
    monkeypatch.setattr(
        alignment,
        "health",
        lambda provider, model, options: calls.append((provider, model, options))
        or (True, "forced align ready"),
    )
    with Session(engine) as session:
        bootstrap_default_profile(session, Settings())
        connection = session.scalar(
            select(ProviderConnection).where(ProviderConnection.key == "align:whisperx")
        )
        model = session.scalar(
            select(ModelCatalogEntry).where(ModelCatalogEntry.connection_id == connection.id)
        )
        result = check_model_health(session, model.id)

    assert result["status"] == "healthy"
    assert result["detail"] == "forced align ready"
    assert calls == [
        (
            "whisperx",
            "wav2vec2-auto",
            {"device": "auto", "interpolate_method": "nearest"},
        )
    ]

def test_pipeline_dispatches_forced_alignment_and_resumes_without_replacing_edits(
    monkeypatch, tmp_path
):
    import localplaud.config as config
    import localplaud.db.session as db_session
    import localplaud.worker.align as alignment
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageAttempt,
        StageName,
        StageStatus,
        TranscriptRevision,
    )
    from localplaud.db.session import init_db, session_scope
    from localplaud.providers.service import (
        create_profile_version,
        list_profiles,
        select_recording_override,
    )
    from localplaud.worker.pipeline import _persist_aligned_transcript, process_file

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'align.db'}")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONVERT", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__DIARIZE", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__POLISH", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__SUMMARIZE", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__MIND_MAP", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__INDEX", "false")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    config.get_settings(reload=True)
    init_db()
    audio = tmp_path / "forced.wav"
    audio.write_bytes(b"RIFF")
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="forced",
                filename="Forced alignment",
                status=FileStatus.downloaded,
                audio_path=str(audio),
            )
        )
        session.flush()
        base = list_profiles(session)[0]
        stages = dict(base["stages"])
        stages["align"] = {
            "connection": "align:whisperx",
            "model": "wav2vec2-auto",
            "options": {"device": "cuda"},
        }
        profile = create_profile_version(
            session,
            {
                "key": "forced-align-test",
                "name": "Forced align test",
                "privacy_policy": "local-only",
                "no_egress": True,
                "stages": stages,
            },
        )
        select_recording_override(session, "forced", profile["id"])

    monkeypatch.setattr(
        "localplaud.worker.pipeline.transcribe.run_asr",
        lambda *_args: Transcript(
            segments=[Segment(text="hello world", start=0, end=1)],
            language="en",
            provider="faster-whisper",
            model="large-v3-turbo",
        ),
    )
    calls = []

    def fake_forced_align(_audio, transcript, *, model, options):
        calls.append((model, options))
        return alignment.AlignmentResult(
            transcript=Transcript(
                segments=[
                    Segment(
                        text=transcript.segments[0].text,
                        start=0.1,
                        end=0.9,
                        words=[
                            Word(text="hello", start=0.1, end=0.4, confidence=0.9),
                            Word(text="world", start=0.5, end=0.9, confidence=0.8),
                        ],
                    )
                ],
                language=transcript.language,
                provider=transcript.provider,
                model=transcript.model,
            ),
            provider="whisperx",
            model=model,
            detail={
                "strategy": "whisperx-wav2vec2",
                "forced_alignment": True,
                "word_count": 2,
                "timed_segments": 1,
                "segment_count": 1,
                "segment_coverage": 1.0,
                "device": options["device"],
            },
        )

    monkeypatch.setattr(alignment, "_forced_align_whisperx", fake_forced_align)
    process_file("forced")

    with session_scope() as session:
        row = session.get(PlaudFile, "forced")
        raw = row.local_transcript
        transcript_id = raw.id
        assert [word["text"] for word in raw.segments[0]["words"]] == ["hello", "world"]
        run = next(item for item in row.stage_runs if item.stage == StageName.align)
        assert run.status == StageStatus.completed
        assert (run.provider, run.model) == ("whisperx", "wav2vec2-auto")
        assert run.detail["forced_alignment"] is True
        assert run.resolved_profile_snapshot["stages"]["align"]["connection"] == "align:whisperx"
        attempt = session.query(StageAttempt).filter_by(
            file_id="forced", stage=StageName.align
        ).one()
        assert attempt.status == StageStatus.completed
        session.add(
            TranscriptRevision(
                file_id="forced",
                base_transcript_id=transcript_id,
                revision=1,
                source="local",
                text="edited",
                segments=[{"text": "edited", "start": 0.1, "end": 0.9}],
            )
        )

    _persist_aligned_transcript(
        "forced",
        Transcript(
            segments=[
                Segment(
                    text="hello world",
                    start=0.12,
                    end=0.92,
                    words=[Word(text="hello world", start=0.12, end=0.92)],
                )
            ],
            language="en",
            provider="faster-whisper",
            model="large-v3-turbo",
        ),
    )
    with session_scope() as session:
        row = session.get(PlaudFile, "forced")
        assert row.local_transcript.id == transcript_id
        assert row.transcript_revisions[0].base_transcript_id == transcript_id

    process_file("forced")
    assert len(calls) == 1
    assert calls[0][0] == "wav2vec2-auto"
    assert calls[0][1]["device"] == "cuda"
    assert calls[0][1]["interpolate_method"] == "nearest"
    with session_scope() as session:
        run = next(
            item for item in session.get(PlaudFile, "forced").stage_runs
            if item.stage == StageName.align
        )
        assert run.attempts == 1
