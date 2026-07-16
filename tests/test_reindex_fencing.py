from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest
from sqlalchemy import delete, select, text


def _database(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.session import init_db

    monkeypatch.setenv(
        "LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'reindex.db'}"
    )
    monkeypatch.setenv("LOCALPLAUD_POLLER__DOWNLOAD_DIR", str(tmp_path / "recordings"))
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    settings = get_settings(reload=True)
    init_db()
    return settings


def _seed(*, display_name: str | None = None) -> None:
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        Speaker,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
        TranscriptRevision,
    )
    from localplaud.db.session import session_scope

    segments = [
        {
            "text": "revision one",
            "start": 0.0,
            "end": 1.0,
            "speaker": "SPEAKER_00",
            "words": [
                {
                    "text": "revision one",
                    "start": 0.0,
                    "end": 1.0,
                    "speaker": "SPEAKER_00",
                    "confidence": 0.9,
                }
            ],
        }
    ]
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="race",
                filename="Race",
                status=FileStatus.done,
                duration_ms=1000,
            )
        )
        raw = Transcript(
            file_id="race",
            provider="test",
            source="local",
            text="raw text",
            segments=segments,
            has_speakers=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            TranscriptRevision(
                file_id="race",
                base_transcript_id=raw.id,
                revision=1,
                source="local",
                text="revision one",
                segments=segments,
                has_speakers=True,
            )
        )
        session.add(
            Speaker(
                file_id="race",
                key="SPEAKER_00",
                display_name=display_name,
            )
        )
        session.add(
            StageRun(
                file_id="race",
                stage=StageName.index,
                status=StageStatus.pending,
                attempts=0,
                detail={"stale": True, "stale_generation": "revision-1"},
            )
        )


def _commit_revision_two(*, speaker_name: str | None = None) -> None:
    from localplaud.db.models import (
        Chunk,
        Speaker,
        StageName,
        StageRun,
        StageStatus,
        TranscriptRevision,
    )
    from localplaud.db.session import session_scope

    segments = [
        {
            "text": "revision two",
            "start": 0.0,
            "end": 1.0,
            "speaker": "SPEAKER_00",
            "words": [],
        }
    ]
    with session_scope() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        first = session.scalar(
            select(TranscriptRevision).where(
                TranscriptRevision.file_id == "race",
                TranscriptRevision.revision == 1,
            )
        )
        session.add(
            TranscriptRevision(
                file_id="race",
                base_transcript_id=first.base_transcript_id,
                revision=2,
                source="local",
                text="revision two",
                segments=segments,
                has_speakers=True,
            )
        )
        if speaker_name is not None:
            speaker = session.scalar(
                select(Speaker).where(
                    Speaker.file_id == "race", Speaker.key == "SPEAKER_00"
                )
            )
            speaker.display_name = speaker_name
        session.execute(delete(Chunk).where(Chunk.file_id == "race"))
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "race", StageRun.stage == StageName.index
            )
        )
        run.status = StageStatus.pending
        run.error = None
        run.completed_at = None
        run.detail = {"stale": True, "stale_generation": "revision-2"}


def _rename_speaker(display_name: str) -> None:
    from localplaud.db.models import Speaker, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope

    with session_scope() as session:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        speaker = session.scalar(
            select(Speaker).where(
                Speaker.file_id == "race", Speaker.key == "SPEAKER_00"
            )
        )
        speaker.display_name = display_name
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "race", StageRun.stage == StageName.index
            )
        )
        run.status = StageStatus.pending
        run.error = None
        run.completed_at = None
        run.detail = {"stale": True, "stale_generation": "speaker-name-2"}


def test_superseded_reindex_cannot_publish_or_complete_stage(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    _seed()

    import localplaud.worker.index as index_module
    from localplaud.db.models import Chunk, StageAttempt, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker.reindex import reindex_file

    embedding_started = Event()
    release_embedding = Event()

    def blocked_embed(chunks, _settings):
        assert [chunk["text"] for chunk in chunks] == ["revision one"]
        embedding_started.set()
        assert release_embedding.wait(5)
        return [b"\x00\x00\x80?"], "fake", 1

    monkeypatch.setattr(index_module, "embed_chunks", blocked_embed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_job = pool.submit(
            reindex_file,
            "race",
            settings,
            expected_revision=1,
            expected_speaker_names={},
        )
        assert embedding_started.wait(5)
        _commit_revision_two()
        release_embedding.set()
        assert old_job.result(timeout=5) is False

    with session_scope() as session:
        assert list(session.scalars(select(Chunk).where(Chunk.file_id == "race"))) == []
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "race", StageRun.stage == StageName.index
            )
        )
        attempt = session.scalar(
            select(StageAttempt).where(
                StageAttempt.file_id == "race", StageAttempt.stage == StageName.index
            )
        )
        assert run.status == StageStatus.pending
        assert run.detail == {"stale": True, "stale_generation": "revision-2"}
        assert run.error is None
        assert attempt.status == StageStatus.skipped
        assert "superseded" in attempt.error
        assert attempt.latency_ms is not None
        assert attempt.usage.get("process_peak_memory_mb") is not None
        assert attempt.estimated_cost_usd == 0
        assert attempt.provider == settings.embeddings.provider
        assert attempt.model == "fake"


def test_superseded_reindex_settles_dispatch_reservation_once(monkeypatch, tmp_path):
    _database(monkeypatch, tmp_path)
    _seed()

    from datetime import UTC, datetime, timedelta

    from localplaud.db.models import (
        ProviderCostReservation,
        StageAttempt,
        StageName,
        StageStatus,
    )
    from localplaud.db.session import session_scope
    from localplaud.providers.usage import cost_budget_status
    from localplaud.worker.reindex import _skip_superseded_attempt

    reservation_id = "stage:reindex:embed:1"
    with session_scope() as session:
        session.add(
            StageAttempt(
                file_id="race",
                stage=StageName.index,
                attempt=1,
                status=StageStatus.running,
                usage={"dispatch_reservation_ids": [reservation_id]},
                estimated_cost_usd=0,
                started_at=datetime.now(UTC),
            )
        )
        session.add(
            ProviderCostReservation(
                id=reservation_id,
                scope_key="file:race",
                file_id="race",
                operation="embed",
                status="active",
                owner="old-worker",
                lease_until=datetime.now(UTC) + timedelta(hours=1),
                estimated_cost_usd=0.25,
            )
        )

    with session_scope() as session:
        _skip_superseded_attempt(session, "race", 1)

    with session_scope() as session:
        attempt = session.scalar(select(StageAttempt))
        assert attempt.status == StageStatus.skipped
        assert attempt.usage["dispatch_reservation_ids"] == [reservation_id]
        assert attempt.estimated_cost_usd == pytest.approx(0.25)
        assert session.get(ProviderCostReservation, reservation_id) is None
        budget = cost_budget_status(session, "race", {"policy": {}})
        assert budget["stage_spent_usd"] == pytest.approx(0.25)
        assert budget["reserved_usd"] == 0
        assert budget["spent_usd"] == pytest.approx(0.25)


def test_postgresql_reindex_writes_lock_library_before_recording_rows():
    from localplaud.worker.reindex import _lock_reindex_write_rows

    events: list[str] = []

    class Bind:
        class dialect:
            name = "postgresql"

    class Session:
        def get_bind(self):
            return Bind()

        def execute(self, statement):
            events.append(str(statement))

        def scalar(self, statement):
            events.append(str(statement))

    _lock_reindex_write_rows(Session(), "race")

    assert events[0] == "SELECT pg_advisory_xact_lock_shared(1280330574)"
    assert "plaud_files.id = :id_1" in events[1]
    assert "FOR UPDATE" in events[1]
    assert "stage_runs.id" in events[2]
    assert "FOR UPDATE" in events[2]


def test_canonical_text_and_lineage_are_one_locked_snapshot(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    _seed()

    import localplaud.worker.index as index_module
    import localplaud.worker.reindex as reindex_module
    from localplaud.db.models import Chunk
    from localplaud.db.session import session_scope

    raw_loaded = Event()
    release_snapshot = Event()
    embedding_started = Event()
    release_embedding = Event()
    real_select = reindex_module._select_raw_transcript

    def pause_after_raw_load(row, selected_settings):
        raw = real_select(row, selected_settings)
        raw_loaded.set()
        assert release_snapshot.wait(5)
        return raw

    def blocked_embed(chunks, _settings):
        assert [chunk["text"] for chunk in chunks] == ["revision one"]
        embedding_started.set()
        assert release_embedding.wait(5)
        return [b"\x00\x00\x80?"], "fake", 1

    monkeypatch.setattr(reindex_module, "_select_raw_transcript", pause_after_raw_load)
    monkeypatch.setattr(index_module, "embed_chunks", blocked_embed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old_job = pool.submit(reindex_module.reindex_file, "race", settings)
        assert raw_loaded.wait(5)
        writer = pool.submit(_commit_revision_two)
        with pytest.raises(TimeoutError):
            writer.result(timeout=0.1)
        release_snapshot.set()
        assert embedding_started.wait(5)
        writer.result(timeout=5)
        release_embedding.set()
        assert old_job.result(timeout=5) is False

    with session_scope() as session:
        assert list(session.scalars(select(Chunk).where(Chunk.file_id == "race"))) == []


def test_speaker_name_generation_fences_publish(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    _seed(display_name="Alice")

    import localplaud.worker.index as index_module
    from localplaud.db.models import Chunk, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker.reindex import reindex_file

    embedding_started = Event()
    release_embedding = Event()

    def blocked_embed(chunks, _settings):
        embedding_started.set()
        assert release_embedding.wait(5)
        return [b"\x00\x00\x80?"], "fake", 1

    monkeypatch.setattr(index_module, "embed_chunks", blocked_embed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_job = pool.submit(
            reindex_file,
            "race",
            settings,
            expected_revision=1,
            expected_speaker_names={"SPEAKER_00": "Alice"},
        )
        assert embedding_started.wait(5)
        _rename_speaker("Bob")
        release_embedding.set()
        assert old_job.result(timeout=5) is False

    with session_scope() as session:
        assert list(session.scalars(select(Chunk).where(Chunk.file_id == "race"))) == []
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "race", StageRun.stage == StageName.index
            )
        )
        assert run.status == StageStatus.pending
        assert run.detail["stale_generation"] == "speaker-name-2"


def test_reindex_keeps_stable_speaker_key_for_named_scope(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    _seed(display_name="Alice")

    import localplaud.worker.index as index_module
    import localplaud.worker.qa as qa_module
    from localplaud.db.models import Chunk
    from localplaud.db.session import session_scope
    from localplaud.worker.reindex import reindex_file

    monkeypatch.setattr(
        index_module,
        "embed_chunks",
        lambda chunks, _settings: ([b"\x00\x00\x80?"], "fake", 1),
    )
    assert reindex_file(
        "race",
        settings,
        expected_revision=1,
        expected_speaker_names={"SPEAKER_00": "Alice"},
    )
    with session_scope() as session:
        chunk = session.scalar(select(Chunk).where(Chunk.file_id == "race"))
        assert chunk.speaker == "SPEAKER_00"
        assert chunk.input_transcript_revision == 1

    class FakeEmbedder:
        def embed(self, _texts):
            return [[1.0]]

    monkeypatch.setattr(qa_module, "build_embedder", lambda _settings: FakeEmbedder())
    hits = qa_module.retrieve(
        "revision",
        settings=settings,
        retrieval_scope={"speaker_name": "Alice"},
    )
    assert [hit["text"] for hit in hits] == ["revision one"]


def test_reindex_refuses_an_active_recording_claim(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    settings = _database(monkeypatch, tmp_path)
    _seed()
    import localplaud.worker.index as index_module
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.worker.reindex import reindex_file

    with session_scope() as session:
        recording = session.get(PlaudFile, "race")
        recording.processing_token = "another-process"
        recording.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)
    monkeypatch.setattr(
        index_module,
        "embed_chunks",
        lambda *_args, **_kwargs: pytest.fail("provider must not run without the claim"),
    )
    assert reindex_file("race", settings) is False


def test_displaced_reindex_cannot_publish_or_complete(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    settings = _database(monkeypatch, tmp_path)
    _seed()
    import localplaud.worker.index as index_module
    from localplaud.db.models import (
        Chunk,
        PlaudFile,
        ProviderCostReservation,
        StageAttempt,
        StageName,
        StageRun,
        StageStatus,
    )
    from localplaud.db.session import session_scope
    from localplaud.worker.reindex import reindex_file

    provider_started = Event()
    provider_return = Event()

    def delayed_embed(chunks, _settings):
        provider_started.set()
        assert provider_return.wait(5)
        return [b"\x00\x00\x80?"] * len(chunks), "old-space", 1

    monkeypatch.setattr(index_module, "embed_chunks", delayed_embed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_worker = pool.submit(reindex_file, "race", settings)
        assert provider_started.wait(5)
        with session_scope() as session:
            attempt = session.scalar(
                select(StageAttempt).where(
                    StageAttempt.file_id == "race",
                    StageAttempt.stage == StageName.index,
                    StageAttempt.status == StageStatus.running,
                )
            )
            reservation_id = "stage:race:embed:displaced"
            attempt.usage = {"dispatch_reservation_ids": [reservation_id]}
            attempt.estimated_cost_usd = 0.25
            session.add(
                ProviderCostReservation(
                    id=reservation_id,
                    scope_key="file:race",
                    file_id="race",
                    operation="embed",
                    status="active",
                    lease_until=datetime.now(UTC) + timedelta(minutes=5),
                    estimated_cost_usd=0.25,
                )
            )
            recording = session.get(PlaudFile, "race")
            recording.processing_token = "new-reindex-owner"
            recording.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)
        provider_return.set()
        assert old_worker.result(timeout=5) is False

    with session_scope() as session:
        recording = session.get(PlaudFile, "race")
        assert recording.processing_token == "new-reindex-owner"
        assert session.scalar(select(Chunk.id).where(Chunk.file_id == "race")) is None
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "race", StageRun.stage == StageName.index
            )
        )
        assert run.status == StageStatus.running
        attempt = session.scalar(
            select(StageAttempt).where(
                StageAttempt.file_id == "race", StageAttempt.stage == StageName.index
            )
        )
        assert attempt.status == StageStatus.skipped
        assert attempt.estimated_cost_usd == 0.25
        assert session.get(ProviderCostReservation, reservation_id) is None


@pytest.mark.parametrize(
    ("returned_model", "message"),
    [(None, "returned no model"), ("wrong-space", "different model")],
)
def test_remote_reindex_requires_exact_returned_model(
    monkeypatch, tmp_path, returned_model, message
):
    import base64

    import numpy as np

    import localplaud.worker.reindex as reindex
    from localplaud.asr.base import Segment, Transcript

    settings = _database(monkeypatch, tmp_path)
    snapshot = {
        "stages": {
            "embed": {
                "connection": "worker:gpu",
                "model": "requested-space",
                "execution_target": "remote_worker",
            }
        },
        "policy": {},
    }
    transcript = Transcript(segments=[Segment(text="Grounded", start=0, end=1)])
    chunks = [{"text": "Grounded", "start": 0, "end": 1, "speaker": None}]
    monkeypatch.setattr(reindex, "_cost_guard", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        reindex,
        "_run_remote_stage",
        lambda *_args, **_kwargs: {
            "chunks": chunks,
            "vectors_base64": [
                base64.b64encode(np.asarray([1.0], dtype=np.float32).tobytes()).decode()
            ],
            "model": returned_model,
            "dim": 1,
        },
    )
    with pytest.raises(ValueError, match=message):
        reindex._embed_reindex_chunks(
            "race", transcript, chunks, settings, snapshot
        )


def test_pending_scanner_recovers_abandoned_running_reindex(monkeypatch, tmp_path):
    from datetime import UTC, datetime

    settings = _database(monkeypatch, tmp_path)
    _seed()
    import localplaud.worker.index as index_module
    from localplaud.db.models import StageAttempt, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker.reindex import process_pending_reindexes

    with session_scope() as session:
        run = session.scalar(
            select(StageRun).where(
                StageRun.file_id == "race", StageRun.stage == StageName.index
            )
        )
        run.status = StageStatus.running
        run.attempts = 1
        run.detail = dict(run.detail or {}) | {"reindex_only": True}
        session.add(
            StageAttempt(
                file_id="race",
                stage=StageName.index,
                attempt=1,
                status=StageStatus.running,
                started_at=datetime.now(UTC),
            )
        )
    monkeypatch.setattr(
        index_module,
        "embed_chunks",
        lambda chunks, _settings: ([b"\x00\x00\x80?"] * len(chunks), "fake", 1),
    )
    assert process_pending_reindexes(settings, limit=1) == 1
    with session_scope() as session:
        attempts = list(
            session.scalars(
                select(StageAttempt)
                .where(StageAttempt.file_id == "race")
                .order_by(StageAttempt.attempt)
            )
        )
        assert [attempt.status for attempt in attempts] == [
            StageStatus.skipped,
            StageStatus.completed,
        ]
        assert all(attempt.completed_at is not None for attempt in attempts)
