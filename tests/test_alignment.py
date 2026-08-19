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


def test_word_timestamps_may_overlap_across_ordered_segments():
    transcript = Transcript(
        segments=[
            Segment(
                text="first",
                start=0,
                end=1.2,
                words=[Word(text="first", start=1.1, end=1.2)],
            ),
            Segment(
                text="second",
                start=1.0,
                end=2.0,
                words=[Word(text="second", start=1.0, end=1.4)],
            ),
        ]
    )

    detail = inspect_word_alignment(transcript)

    assert detail["cross_segment_word_overlaps"] == 1
    assert detail["segment_coverage"] == 1.0


def test_untimed_asr_evidence_does_not_corrupt_timed_segment_chronology():
    transcript = Transcript(
        segments=[
            Segment(
                text="before",
                start=10,
                end=11,
                words=[Word(text="before", start=10, end=11)],
            ),
            Segment(text="!", start=15, end=16),
            Segment(
                text="after",
                start=12,
                end=13,
                words=[Word(text="after", start=12, end=13)],
            ),
        ]
    )

    detail = inspect_word_alignment(transcript)

    assert detail["timed_segments"] == 2
    assert detail["untimed_segments"] == 1
    assert detail["segment_coverage"] == pytest.approx(2 / 3)


def test_standalone_punctuation_is_not_a_speech_chronology_anchor():
    transcript = Transcript(
        segments=[
            Segment(
                text="before",
                start=10,
                end=11,
                words=[Word(text="before", start=10, end=11)],
            ),
            Segment(text="!", start=15, end=16, words=[Word(text="!", start=15, end=16)]),
            Segment(
                text="after",
                start=12,
                end=13,
                words=[Word(text="after", start=12, end=13)],
            ),
        ]
    )

    detail = inspect_word_alignment(transcript)

    assert detail["timed_segments"] == 3
    assert detail["nonlexical_timed_segments"] == 1
    assert detail["segment_coverage"] == 1.0


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
                        "text": "changed text",
                        "start": 0.05,
                        "end": 0.45,
                        "avg_logprob": 0.0,
                        "words": [
                            {"word": "你好", "start": 0.05, "end": 0.45, "score": 0.93},
                        ],
                    },
                    {
                        "text": "must not replace ASR text",
                        "start": 0.5,
                        "end": 0.95,
                        "avg_logprob": 0.0,
                        "words": [
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
        "unaligned_segments": 0,
        "skipped_empty_segments": 0,
        "skipped_short_segments": 0,
        "alignable_segment_count": 1,
    }
    assert calls["load"] == {"language_code": "zh", "device": "cuda"}
    assert calls["align"]["return_char_alignments"] is False


def test_whisperx_preserves_empty_zero_duration_placeholders(monkeypatch, tmp_path):
    import localplaud.worker.align as alignment

    class PlaceholderAwareWhisperX:
        @staticmethod
        def load_align_model(**_kwargs):
            return object(), {}

        @staticmethod
        def load_audio(_path):
            return []

        @staticmethod
        def align(segments, *_args, **_kwargs):
            assert len(segments) == 1
            assert segments[0]["text"] == "spoken"
            return {
                "segments": [
                    {
                        "text": "spoken",
                        "start": 1.0,
                        "end": 2.0,
                        "avg_logprob": 0.0,
                        "words": [{"word": "spoken", "start": 1.0, "end": 2.0}],
                    }
                ]
            }

    monkeypatch.setattr(alignment, "_import_whisperx", lambda: PlaceholderAwareWhisperX)
    monkeypatch.setattr(alignment, "_resolve_device", lambda _requested: "cpu")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    result = run_alignment(
        audio,
        Transcript(
            segments=[
                Segment(text="spoken", start=1, end=2),
                # MLX bookkeeping placeholders can be emitted after a speech
                # segment while retaining an earlier timestamp.
                Segment(text="", start=0, end=0),
            ],
            language="en",
        ),
        provider="whisperx",
        model="wav2vec2-auto",
        options={"device": "cpu", "min_segment_coverage": 1.0},
    )

    assert result.transcript.segments[1].text == ""
    assert result.transcript.segments[1].words == []
    assert result.detail["skipped_empty_segments"] == 1
    assert result.detail["alignable_segment_count"] == 1
    assert result.detail["segment_coverage"] == 1.0


def test_whisperx_preserves_too_short_segments_without_one_frame_trellis(
    monkeypatch, tmp_path
):
    import localplaud.worker.align as alignment

    class ShortSegmentAwareWhisperX:
        @staticmethod
        def load_align_model(**_kwargs):
            return object(), {}

        @staticmethod
        def load_audio(_path):
            return []

        @staticmethod
        def align(segments, *_args, **_kwargs):
            assert len(segments) == 1
            assert segments[0]["text"] == "spoken"
            return {
                "segments": [
                    {
                        "text": "spoken",
                        "start": 0.0,
                        "end": 1.0,
                        "avg_logprob": 0.0,
                        "words": [{"word": "spoken", "start": 0.0, "end": 1.0}],
                    }
                ]
            }

    monkeypatch.setattr(alignment, "_import_whisperx", lambda: ShortSegmentAwareWhisperX)
    monkeypatch.setattr(alignment, "_resolve_device", lambda _requested: "cpu")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    result = run_alignment(
        audio,
        Transcript(
            segments=[
                Segment(text="spoken", start=0, end=1),
                Segment(text="x", start=1, end=1.04),
            ],
            language="en",
        ),
        provider="whisperx",
        model="wav2vec2-auto",
        options={"device": "cpu", "min_segment_coverage": 0.5},
    )

    assert result.transcript.segments[1].text == "x"
    assert result.transcript.segments[1].words == []
    assert result.detail["skipped_short_segments"] == 1
    assert result.detail["unaligned_segments"] == 1
    assert result.detail["segment_coverage"] == 0.5


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
    with pytest.raises(AlignmentError, match="omitted input segment"):
        run_alignment(
            audio,
            Transcript(
                segments=[Segment(text="hello", start=0, end=1)],
                language="en",
            ),
            provider="whisperx",
            model="wav2vec2-auto",
        )


def test_whisperx_preserves_sparse_omissions_subject_to_coverage(monkeypatch, tmp_path):
    import localplaud.worker.align as alignment

    class SparseWhisperX:
        @staticmethod
        def load_align_model(**_kwargs):
            return object(), {}

        @staticmethod
        def load_audio(_path):
            return []

        @staticmethod
        def align(*_args, **_kwargs):
            return {
                "segments": [
                    {
                        "text": "first",
                        "start": 0.1,
                        "end": 0.8,
                        "avg_logprob": 0.0,
                        "words": [{"word": "first", "start": 0.1, "end": 0.8}],
                    }
                ]
            }

    monkeypatch.setattr(alignment, "_import_whisperx", lambda: SparseWhisperX)
    monkeypatch.setattr(alignment, "_resolve_device", lambda _requested: "cpu")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    transcript = Transcript(
        segments=[
            Segment(text="first", start=0, end=1),
            Segment(text="second", start=1, end=2),
        ],
        language="en",
    )

    result = run_alignment(
        audio,
        transcript,
        provider="whisperx",
        model="wav2vec2-auto",
        options={"device": "cpu", "min_segment_coverage": 0.5},
    )

    assert result.transcript.segments[1] == transcript.segments[1]
    assert result.detail["segment_coverage"] == 0.5
    assert result.detail["unaligned_segments"] == 1
    assert result.detail["omitted_segments"] == 1
    assert result.detail["omitted_segment_indexes"] == [1]

    with pytest.raises(AlignmentError, match="coverage 50.0%"):
        run_alignment(
            audio,
            transcript,
            provider="whisperx",
            model="wav2vec2-auto",
            options={"device": "cpu", "min_segment_coverage": 1.0},
        )


def test_whisperx_orders_split_parts_by_timestamp_before_merging_words(
    monkeypatch, tmp_path
):
    import localplaud.worker.align as alignment

    class OutOfOrderPartsWhisperX:
        @staticmethod
        def load_align_model(**_kwargs):
            return object(), {}

        @staticmethod
        def load_audio(_path):
            return []

        @staticmethod
        def align(*_args, **_kwargs):
            return {
                "segments": [
                    {
                        "text": "later",
                        "start": 0.6,
                        "end": 0.9,
                        "avg_logprob": 0.0,
                        "words": [{"word": "later", "start": 0.6, "end": 0.9}],
                    },
                    {
                        "text": "earlier",
                        "start": 0.1,
                        "end": 0.4,
                        "avg_logprob": 0.0,
                        "words": [{"word": "earlier", "start": 0.1, "end": 0.4}],
                    },
                ]
            }

    monkeypatch.setattr(
        alignment, "_import_whisperx", lambda: OutOfOrderPartsWhisperX
    )
    monkeypatch.setattr(alignment, "_resolve_device", lambda _requested: "cpu")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    result = run_alignment(
        audio,
        Transcript(
            segments=[Segment(text="earlier later", start=0, end=1)],
            language="en",
        ),
        provider="whisperx",
        model="wav2vec2-auto",
        options={"device": "cpu", "min_segment_coverage": 1.0},
    )

    assert [word.text for word in result.transcript.segments[0].words] == [
        "earlier",
        "later",
    ]


def test_whisperx_maps_newer_output_without_custom_marker_by_timestamp(monkeypatch, tmp_path):
    import localplaud.worker.align as alignment

    class MarkerDroppingWhisperX:
        @staticmethod
        def load_align_model(**_kwargs):
            return object(), {}

        @staticmethod
        def load_audio(_path):
            return []

        @staticmethod
        def align(*_args, **_kwargs):
            return {
                "segments": [
                    {
                        "text": "first",
                        "start": 0.1,
                        "end": 0.8,
                        "words": [{"word": "first", "start": 0.1, "end": 0.8}],
                    },
                    {
                        "text": "second",
                        "start": 1.2,
                        "end": 1.9,
                        "words": [{"word": "second", "start": 1.2, "end": 1.9}],
                    },
                ]
            }

    monkeypatch.setattr(alignment, "_import_whisperx", lambda: MarkerDroppingWhisperX)
    monkeypatch.setattr(alignment, "_resolve_device", lambda _requested: "cpu")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    result = run_alignment(
        audio,
        Transcript(
            segments=[
                Segment(text="first", start=0, end=1),
                Segment(text="second", start=1, end=2),
            ],
            language="en",
        ),
        provider="whisperx",
        model="wav2vec2-auto",
        options={"device": "cpu", "min_segment_coverage": 1.0},
    )

    assert [segment.text for segment in result.transcript.segments] == ["first", "second"]
    assert result.detail["forced_alignment"] is True
    assert result.detail["timestamp_mapped_segments"] == 2


def test_whisperx_prefers_text_match_between_overlapping_sources(monkeypatch, tmp_path):
    import localplaud.worker.align as alignment

    class TextMatchingWhisperX:
        @staticmethod
        def load_align_model(**_kwargs):
            return object(), {}

        @staticmethod
        def load_audio(_path):
            return []

        @staticmethod
        def align(*_args, **_kwargs):
            return {
                "segments": [
                    {
                        "text": "speech",
                        "start": 0.9,
                        "end": 1.6,
                        "words": [{"word": "speech", "start": 0.9, "end": 1.6}],
                    },
                    {
                        "text": "!",
                        "start": 1.0,
                        "end": 1.5,
                        "words": [{"word": "!", "start": 1.0, "end": 1.5}],
                    },
                ]
            }

    monkeypatch.setattr(alignment, "_import_whisperx", lambda: TextMatchingWhisperX)
    monkeypatch.setattr(alignment, "_resolve_device", lambda _requested: "cpu")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    result = run_alignment(
        audio,
        Transcript(
            segments=[
                Segment(text="speech", start=0.8, end=1.7),
                Segment(text="!", start=1.0, end=1.5),
            ],
            language="en",
        ),
        provider="whisperx",
        model="wav2vec2-auto",
        options={"device": "cpu", "min_segment_coverage": 1.0},
    )

    assert [word.text for word in result.transcript.segments[0].words] == ["speech"]
    assert [word.text for word in result.transcript.segments[1].words] == ["!"]


def test_whisperx_maps_zero_duration_output_to_containing_source(monkeypatch, tmp_path):
    import localplaud.worker.align as alignment

    class PointTimestampWhisperX:
        @staticmethod
        def load_align_model(**_kwargs):
            return object(), {}

        @staticmethod
        def load_audio(_path):
            return []

        @staticmethod
        def align(*_args, **_kwargs):
            return {
                "segments": [
                    {
                        "text": "point",
                        "start": 0.5,
                        "end": 0.5,
                        "words": [{"word": "point", "start": 0.5, "end": 0.5}],
                    }
                ]
            }

    monkeypatch.setattr(alignment, "_import_whisperx", lambda: PointTimestampWhisperX)
    monkeypatch.setattr(alignment, "_resolve_device", lambda _requested: "cpu")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")

    result = run_alignment(
        audio,
        Transcript(segments=[Segment(text="point", start=0, end=1)], language="en"),
        provider="whisperx",
        model="wav2vec2-auto",
        options={"device": "cpu", "min_segment_coverage": 1.0},
    )

    assert result.detail["nearest_mapped_segments"] == 1
    assert result.detail["segment_coverage"] == 1.0

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
        row = session.get(PlaudFile, "forced")
        run = next(
            item for item in row.stage_runs
            if item.stage == StageName.align
        )
        assert run.attempts == 1
        assert (run.provider, run.model) == ("whisperx", "wav2vec2-auto")
        assert row.local_transcript.text == "hello world"
        assert row.transcript_revisions[0].text == "edited"

        session.add(
            PlaudFile(
                id="invalid-align",
                filename="Invalid alignment",
                status=FileStatus.downloaded,
                audio_path=str(audio),
            )
        )
        session.flush()
        select_recording_override(session, "invalid-align", profile["id"])

    def invalid_forced_align(*_args, **_kwargs):
        raise AlignmentError("WhisperX returned invalid timing evidence")

    monkeypatch.setattr(alignment, "_forced_align_whisperx", invalid_forced_align)
    process_file("invalid-align")
    with session_scope() as session:
        row = session.get(PlaudFile, "invalid-align")
        run = next(item for item in row.stage_runs if item.stage == StageName.align)
        assert row.status == FileStatus.partial
        assert row.local_transcript.text == "hello world"
        assert run.status == StageStatus.degraded
        assert run.detail["forced_alignment"] is False
        assert run.detail["requested_forced_alignment"] is True
