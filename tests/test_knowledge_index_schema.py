"""Additive schema and ORM contracts for note-aware knowledge indexing."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from localplaud.db.migrations import migrate_knowledge_index_schema
from localplaud.db.models import (
    Base,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIndexAttempt,
    PlaudFile,
    ProviderCostReservation,
    Summary,
    UserNote,
)


def test_knowledge_index_migration_is_additive_and_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.execute(text("CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE summaries (id INTEGER PRIMARY KEY, "
                "file_id VARCHAR(64) REFERENCES plaud_files(id) ON DELETE CASCADE)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE user_notes (id INTEGER PRIMARY KEY, "
                "file_id VARCHAR(64) REFERENCES plaud_files(id) ON DELETE CASCADE)"
            )
        )
        connection.execute(text("INSERT INTO plaud_files (id) VALUES ('kept')"))

    assert migrate_knowledge_index_schema(engine) == [
        "knowledge_documents",
        "knowledge_chunks",
        "knowledge_index_attempts",
        "provider_cost_reservations",
    ]
    assert migrate_knowledge_index_schema(engine) == []

    inspector = inspect(engine)
    assert connection_scalar(engine, "SELECT id FROM plaud_files") == "kept"
    assert set(inspector.get_table_names()) >= {
        "knowledge_documents",
        "knowledge_chunks",
        "knowledge_index_attempts",
        "provider_cost_reservations",
    }
    document_columns = {
        column["name"] for column in inspector.get_columns("knowledge_documents")
    }
    assert document_columns == {
        "id",
        "kind",
        "file_id",
        "summary_id",
        "user_note_id",
        "artifact_version",
        "content_sha256",
        "generation",
        "status",
        "attempts",
        "lease_token",
        "lease_until",
        "next_retry_at",
        "error",
        "provider",
        "model",
        "dim",
        "profile_snapshot",
        "created_at",
        "updated_at",
        "indexed_at",
    }
    document_indexes = {
        index["name"] for index in inspector.get_indexes("knowledge_documents")
    }
    assert {
        "ix_knowledge_documents_file_id",
        "ix_knowledge_documents_status_next_retry_at",
    } <= document_indexes
    chunk_indexes = {
        index["name"] for index in inspector.get_indexes("knowledge_chunks")
    }
    assert {
        "ix_knowledge_chunks_document_id",
        "ix_knowledge_chunks_dim_document_id",
    } <= chunk_indexes
    attempt_indexes = {
        index["name"] for index in inspector.get_indexes("knowledge_index_attempts")
    }
    assert {
        "ix_knowledge_index_attempts_document_id",
        "ix_knowledge_index_attempts_file_id",
        "ix_knowledge_index_attempts_file_status",
    } <= attempt_indexes
    reservation_indexes = {
        index["name"]
        for index in inspector.get_indexes("provider_cost_reservations")
    }
    reservation_columns = {
        column["name"]
        for column in inspector.get_columns("provider_cost_reservations")
    }
    assert {"owner", "lease_until", "profile_fingerprint"} <= reservation_columns
    assert {
        "ix_provider_cost_reservations_file_id",
        "ix_provider_cost_reservations_lease_until",
        "ix_provider_cost_reservations_owner",
        "ix_provider_cost_reservations_scope_key",
        "ix_provider_cost_reservations_status",
    } <= reservation_indexes
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("knowledge_documents")
    } == {
        "uq_knowledge_documents_summary_id",
        "uq_knowledge_documents_user_note_id",
    }
    assert {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("knowledge_chunks")
    } == {"uq_knowledge_chunks_document_idx"}


def test_knowledge_index_migration_adds_dispatch_lease_to_existing_reservations(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-reservations.db'}")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE provider_cost_reservations (
                id VARCHAR(96) PRIMARY KEY,
                scope_key VARCHAR(128) NOT NULL,
                operation VARCHAR(32) NOT NULL,
                status VARCHAR(20) NOT NULL
            )
        """))
        connection.execute(text("""
            INSERT INTO provider_cost_reservations (id, scope_key, operation, status)
            VALUES ('kept', 'library', 'ask', 'completed')
        """))

    migrated = migrate_knowledge_index_schema(engine)
    assert {
        "provider_cost_reservations.owner",
        "provider_cost_reservations.lease_until",
        "provider_cost_reservations.profile_fingerprint",
    } <= set(migrated)
    assert migrate_knowledge_index_schema(engine) == []
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("provider_cost_reservations")
    }
    assert {"owner", "lease_until", "profile_fingerprint"} <= columns
    assert connection_scalar(
        engine, "SELECT id FROM provider_cost_reservations"
    ) == "kept"


def test_knowledge_document_defaults_constraints_and_database_cascade(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'constraints.db'}")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.execute(
            PlaudFile.__table__.insert().values(id="r1", filename="Meeting")
        )
        connection.execute(
            Summary.__table__.insert().values(
                id=10,
                file_id="r1",
                template="default",
                content_md="body",
                source="local",
            )
        )
        connection.execute(
            text(
                "INSERT INTO knowledge_documents "
                "(kind, file_id, summary_id, content_sha256, generation, "
                "created_at, updated_at) "
                "VALUES ('generated_summary', 'r1', 10, :digest, 'generation-1', "
                ":now, :now)"
            ),
            {"digest": "a" * 64, "now": now},
        )
        row = connection.execute(
            text("SELECT id, status, attempts FROM knowledge_documents")
        ).one()
        assert row.status == "pending"
        assert row.attempts == 0
        connection.execute(
            text(
                "INSERT INTO knowledge_chunks "
                "(document_id, idx, text, created_at) VALUES (:id, 0, 'body', :now)"
            ),
            {"id": row.id, "now": now},
        )

        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO knowledge_documents "
                    "(kind, file_id, content_sha256, generation, created_at, updated_at) "
                    "VALUES ('generated_summary', 'r1', :digest, 'invalid', :now, :now)"
                ),
                {"digest": "b" * 64, "now": now},
            )

    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
        connection.execute(text("DELETE FROM summaries WHERE id = 10"))
        assert connection.scalar(text("SELECT COUNT(*) FROM knowledge_documents")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM knowledge_chunks")) == 0


def test_knowledge_index_orm_relationships_round_trip(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'orm.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        recording = PlaudFile(id="r1", filename="Meeting")
        summary = Summary(template="meeting", content_md="Generated body")
        user_note = UserNote(title="Saved note", content_md="Saved body")
        recording.summaries.append(summary)
        recording.user_notes.append(user_note)
        summary.knowledge_document = KnowledgeDocument(
            kind="generated_summary",
            file=recording,
            content_sha256="a" * 64,
            generation="summary-generation",
            chunks=[KnowledgeChunk(idx=0, text="Generated body")],
        )
        user_note.knowledge_document = KnowledgeDocument(
            kind="user_note",
            file=recording,
            artifact_version=1,
            content_sha256="b" * 64,
            generation="note-generation",
            chunks=[KnowledgeChunk(idx=0, text="Saved body")],
        )
        session.add(recording)
        session.flush()
        session.add(
            KnowledgeIndexAttempt(
                document_id=user_note.knowledge_document.id,
                file_id="r1",
                generation="note-generation",
                attempt=1,
                status="completed",
                estimated_cost_usd=0.01,
            )
        )
        session.add(
            ProviderCostReservation(
                id="ask-1:embed",
                scope_key="file:r1",
                file_id="r1",
                operation="embed",
                status="completed",
                estimated_cost_usd=0.02,
            )
        )
        session.commit()
        summary_id = summary.id
        note_id = user_note.id

    with Session(engine) as session:
        summary = session.get(Summary, summary_id)
        note = session.get(UserNote, note_id)
        assert summary is not None and summary.knowledge_document is not None
        assert note is not None and note.knowledge_document is not None
        assert summary.knowledge_document.file_id == "r1"
        assert summary.knowledge_document.chunks[0].text == "Generated body"
        assert note.knowledge_document.artifact_version == 1
        assert note.knowledge_document.chunks[0].document is note.knowledge_document


def test_concurrent_sqlite_startup_serializes_document_discovery(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.session import init_db, session_scope

    monkeypatch.setenv(
        "LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'startup.db'}"
    )
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()
    with session_scope() as session:
        session.add(UserNote(title="Concurrent", content_md="Body", source_type="manual"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: init_db(), range(2)))
    assert len(results) == 2
    with session_scope() as session:
        assert session.query(KnowledgeDocument).count() == 1


def connection_scalar(engine, statement: str):
    with engine.connect() as connection:
        return connection.scalar(text(statement))
