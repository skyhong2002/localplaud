"""Custom vocabulary preserves raw ASR and creates traceable corrections."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, select, text


def test_legacy_vocabulary_schema_is_rebuilt_without_losing_rules(tmp_path):
    from localplaud.db.migrations import migrate_vocabulary_schema

    engine = create_engine(f"sqlite:///{tmp_path/'legacy-vocabulary.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """CREATE TABLE vocabulary_terms (
                    id INTEGER PRIMARY KEY,
                    term VARCHAR(256) NOT NULL UNIQUE,
                    replacement VARCHAR(256) NOT NULL,
                    language VARCHAR(16),
                    case_sensitive BOOLEAN NOT NULL,
                    enabled BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO vocabulary_terms
                    (id, term, replacement, language, case_sensitive, enabled, created_at, updated_at)
                    VALUES (9, 'old', 'new', 'zh-TW', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"""
            )
        )

    assert migrate_vocabulary_schema(engine) == ["vocabulary_terms"]
    columns = {item["name"] for item in inspect(engine).get_columns("vocabulary_terms")}
    assert {"source_text", "replacement_text"} <= columns
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id, source_text, replacement_text, language, case_sensitive, enabled "
                "FROM vocabulary_terms"
            )
        ).one()
    assert tuple(row) == (9, "old", "new", "zh-TW", 1, 1)
    assert migrate_vocabulary_schema(engine) == []


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'vocab.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed():
    from localplaud.db.models import (
        Chunk,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
        VocabularyTerm,
    )
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            PlaudFile(id="meeting", filename="研究會議", origin="local", start_time_ms=1000)
        )
        session.add(
            Transcript(
                file_id="meeting",
                provider="test-asr",
                language="zh-TW",
                source="local",
                text="歐米觀察 uses open AI",
                segments=[
                    {
                        "text": "歐米觀察 uses open AI",
                        "start": 3.0,
                        "end": 7.0,
                        "speaker": "SPEAKER_00",
                        "words": [{"text": "歐米觀察", "start": 3.0, "end": 4.0}],
                    }
                ],
            )
        )
        session.add_all(
            [
                VocabularyTerm(
                    source_text="歐米觀察", replacement_text="OmniObserve", language="zh"
                ),
                VocabularyTerm(
                    source_text="open AI",
                    replacement_text="OpenAI",
                    language=None,
                    case_sensitive=False,
                ),
                VocabularyTerm(
                    source_text="uses", replacement_text="wrong", language="en", enabled=True
                ),
            ]
        )
        session.add(Chunk(file_id="meeting", idx=0, text="stale", start=0, end=1))
        session.add(
            StageRun(
                file_id="meeting",
                stage=StageName.summarize,
                status=StageStatus.completed,
                attempts=1,
            )
        )


def test_apply_vocabulary_creates_revision_and_preserves_raw(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import Chunk, PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.vocabulary import apply_vocabulary

    result = apply_vocabulary("meeting", automatic=True)
    assert result["replacements"] == 2
    assert result["revision"] == 1

    with session_scope() as session:
        row = session.get(PlaudFile, "meeting")
        raw = row.local_transcript
        corrected = row.corrected_transcript_for_source("local")
        assert raw.text == "歐米觀察 uses open AI"
        assert corrected.text == "OmniObserve uses OpenAI"
        assert corrected.segments[0]["words"] == []
        assert corrected.note.startswith("vocabulary:auto rules=")
        assert session.scalars(select(Chunk).where(Chunk.file_id == "meeting")).all() == []
        summary = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "meeting", StageRun.stage == StageName.summarize
            )
        )
        assert summary.status == StageStatus.pending
        assert summary.detail["reason"] == "vocabulary"

    skipped = apply_vocabulary("meeting", automatic=True)
    assert skipped["skipped"] == "existing correction"


def test_explicit_apply_stacks_on_existing_revision(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import PlaudFile, VocabularyTerm
    from localplaud.db.session import session_scope
    from localplaud.vocabulary import apply_vocabulary

    assert apply_vocabulary("meeting", automatic=True)["revision"] == 1
    with session_scope() as session:
        session.add(VocabularyTerm(source_text="uses", replacement_text="採用", language="zh"))
    result = apply_vocabulary("meeting")
    assert result["revision"] == 2
    with session_scope() as session:
        corrected = session.get(PlaudFile, "meeting").corrected_transcript_for_source("local")
        assert corrected.text == "OmniObserve 採用 OpenAI"
        assert corrected.note.startswith("vocabulary:manual")


def test_vocabulary_api_and_settings_surface(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    payload = {
        "source_text": "歐米觀察",
        "replacement_text": "OmniObserve",
        "language": "zh_TW",
        "case_sensitive": False,
        "enabled": True,
    }
    created = client.post("/api/vocabulary", json=payload)
    assert created.status_code == 201
    term_id = created.json()["id"]
    assert created.json()["language"] == "zh-TW"
    assert client.post("/api/vocabulary", json=payload).status_code == 409
    assert client.get("/api/vocabulary").json()["terms"][0]["source_text"] == "歐米觀察"
    page = client.get("/settings")
    assert page.status_code == 200
    assert "Custom vocabulary" in page.text and "OmniObserve" in page.text

    payload["replacement_text"] = "Omni Observe"
    assert client.put(f"/api/vocabulary/{term_id}", json=payload).status_code == 200
    assert client.delete(f"/api/vocabulary/{term_id}").status_code == 200
    assert client.get("/api/vocabulary").json()["terms"] == []


def test_corrections_do_not_cascade_or_overwrite_longer_matches(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    from localplaud.db.models import VocabularyTerm
    from localplaud.vocabulary import correct_segments

    terms = [
        VocabularyTerm(id=1, source_text="open AI", replacement_text="OpenAI"),
        VocabularyTerm(id=2, source_text="AI", replacement_text="人工智慧"),
    ]
    segments, count, rules = correct_segments(
        [{"text": "open AI and AI", "start": 0, "end": 2}], terms, "en"
    )
    assert segments[0]["text"] == "OpenAI and 人工智慧"
    assert count == 2 and rules == [1, 2]


def test_pipeline_automatically_applies_vocabulary_after_asr(monkeypatch, tmp_path):
    from localplaud.asr.base import Segment, Transcript

    client = _client(monkeypatch, tmp_path)
    assert client.post(
        "/api/vocabulary",
        json={"source_text": "hello world", "replacement_text": "Hello, Localplaud!"},
    ).status_code == 201
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__CONVERT", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__DIARIZE", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__SUMMARIZE", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__MIND_MAP", "false")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__INDEX", "false")
    from localplaud.config import get_settings

    get_settings(reload=True)
    audio = tmp_path / "recording.wav"
    audio.write_bytes(b"RIFFfake")
    from localplaud.db.models import FileStatus, PlaudFile, StageName
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            PlaudFile(
                id="pipeline-vocab",
                filename="Pipeline",
                status=FileStatus.downloaded,
                audio_path=str(audio),
            )
        )
    monkeypatch.setattr(
        "localplaud.worker.pipeline.transcribe.run_asr",
        lambda *_args, **_kwargs: Transcript(
            segments=[Segment(text="hello world", start=0, end=1)],
            language="en",
            provider="fake",
        ),
    )
    from localplaud.worker.pipeline import process_file

    process_file("pipeline-vocab")
    with session_scope() as session:
        row = session.get(PlaudFile, "pipeline-vocab")
        assert row.local_transcript.text == "hello world"
        assert row.corrected_transcript.text == "Hello, Localplaud!"
        run = next(item for item in row.stage_runs if item.stage == StageName.transcribe)
        assert run.detail["vocabulary"]["replacements"] == 1


def test_vocabulary_migration_is_idempotent(tmp_path):
    from sqlalchemy import create_engine, inspect

    from localplaud.db.migrations import migrate_vocabulary_schema

    engine = create_engine(f"sqlite:///{tmp_path/'legacy.db'}")
    assert migrate_vocabulary_schema(engine) == ["vocabulary_terms"]
    assert "vocabulary_terms" in inspect(engine).get_table_names()
    assert migrate_vocabulary_schema(engine) == []
