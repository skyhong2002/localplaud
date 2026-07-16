from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import select


def _database(monkeypatch, tmp_path):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.session import init_db

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'knowledge.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    settings = get_settings(reload=True)
    init_db()
    return settings


def _recording(session, file_id: str, title: str):
    from localplaud.db.models import FileStatus, PlaudFile, Transcript

    row = PlaudFile(id=file_id, filename=title, status=FileStatus.done, origin="local")
    session.add(row)
    transcript = Transcript(
        file_id=file_id,
        provider="test",
        model="test",
        source="local",
        text=f"Transcript for {title}",
        segments=[{"text": title, "start": 0.0, "end": 1.0, "speaker": None}],
    )
    session.add(transcript)
    session.flush()
    return row, transcript


class _QueryEmbedder:
    name = "fake"

    def embed(self, _texts):
        return [[1.0, 0.0]]


def _complete_document(session, document, text: str, vector=(1.0, 0.0)):
    from localplaud.db.models import KnowledgeChunk
    from localplaud.providers.service import preview_resolution, resolve_recording_profile

    document.status = "completed"
    document.provider = "fake"
    document.model = "fake"
    document.dim = 2
    document.profile_snapshot = (
        resolve_recording_profile(session, document.file_id).to_dict()
        if document.file_id
        else preview_resolution(session).to_dict()
    )
    document.indexed_at = datetime.now(UTC)
    session.add(
        KnowledgeChunk(
            document_id=document.id,
            idx=0,
            text=text,
            embedding_model="fake",
            dim=2,
            embedding=np.asarray(vector, dtype=np.float32).tobytes(),
        )
    )


def test_library_and_file_ask_retrieve_current_generated_and_saved_notes(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    from localplaud.db.models import Summary, UserNote
    from localplaud.db.session import session_scope
    from localplaud.worker.knowledge_index import (
        sync_summary_document,
        sync_user_note_document,
    )

    with session_scope() as session:
        _, transcript = _recording(session, "r1", "Weekly Sync")
        _recording(session, "r2", "Interview")
        summary = Summary(
            file_id="r1",
            template="meeting",
            title="Decisions",
            content_md="Ship the beta on Friday.",
            source="local",
            input_transcript_id=transcript.id,
            input_transcript_revision=0,
            input_transcript_source="local",
        )
        saved = UserNote(
            file_id="r1",
            title="Follow up",
            content_md="Call Riley tomorrow.",
            source_type="manual",
        )
        library_note = UserNote(
            file_id=None,
            title="Cross-library answer",
            content_md="The common deadline is Friday.",
            source_type="ask",
        )
        session.add_all([summary, saved, library_note])
        session.flush()
        generated_doc = sync_summary_document(session, summary, settings)
        saved_doc = sync_user_note_document(session, saved, settings)
        library_doc = sync_user_note_document(session, library_note, settings)
        _complete_document(session, generated_doc, "Decisions\n\nShip the beta on Friday.")
        _complete_document(session, saved_doc, "Follow up\n\nCall Riley tomorrow.")
        _complete_document(
            session, library_doc, "Cross-library answer\n\nThe common deadline is Friday."
        )

    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda _settings: _QueryEmbedder())
    from localplaud.worker.qa import retrieve

    library_hits = retrieve("Friday", settings=settings, top_k=10)
    assert {hit["target"] for hit in library_hits} >= {"generated_note", "saved_note"}
    generated = next(hit for hit in library_hits if hit["target"] == "generated_note")
    assert generated["url"] == "/file/r1/notes/generated/meeting/versions/1"
    assert generated["start"] is None and generated["speaker"] is None
    assert any(hit["file_id"] is None for hit in library_hits)

    file_hits = retrieve("Friday", settings=settings, file_id="r1", top_k=10)
    assert file_hits
    assert all(hit["file_id"] == "r1" for hit in file_hits)
    assert not any(hit["artifact_title"] == "Cross-library answer" for hit in file_hits)

    scoped = retrieve("Friday", settings=settings, retrieval_scope={"file_ids": ["r1"]}, top_k=10)
    assert not any(hit["file_id"] is None for hit in scoped)
    assert (
        retrieve("Friday", settings=settings, retrieval_scope={"speaker_name": "Sky"}, top_k=10)
        == []
    )


def test_generated_note_eligibility_fails_closed(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    from localplaud.db.models import StageName, StageRun, StageStatus, Summary, UserNote
    from localplaud.db.session import session_scope
    from localplaud.worker.knowledge_index import (
        sync_summary_document,
        sync_user_note_document,
    )

    with session_scope() as session:
        _, transcript = _recording(session, "r1", "Weekly Sync")
        cloud = Summary(file_id="r1", template="cloud", content_md="paid", source="cloud")
        mismatch = Summary(
            file_id="r1",
            template="mismatch",
            content_md="old",
            source="local",
            input_transcript_id=transcript.id + 100,
            input_transcript_revision=0,
            input_transcript_source="local",
        )
        ambiguous_copy = UserNote(
            file_id="r1",
            title="Legacy copy",
            content_md="unknown source",
            source_type="generated_summary",
            source_summary_snapshot={"template": "legacy"},
        )
        forged_partial = UserNote(
            file_id="r1",
            title="Partial provenance",
            content_md="missing fingerprint and lineage ids",
            source_type="generated_summary",
            source_summary_snapshot={
                "source": "local",
                "input_transcript_source": "local",
            },
        )
        session.add_all([cloud, mismatch, ambiguous_copy, forged_partial])
        session.flush()
        assert sync_summary_document(session, cloud, settings) is None
        assert sync_summary_document(session, mismatch, settings) is None
        assert sync_user_note_document(session, ambiguous_copy, settings) is None
        assert sync_user_note_document(session, forged_partial, settings) is None

        current = Summary(
            file_id="r1",
            template="meeting",
            content_md="current",
            source="local",
            input_transcript_id=transcript.id,
            input_transcript_revision=0,
            input_transcript_source="local",
        )
        session.add(current)
        session.flush()
        assert sync_summary_document(session, current, settings) is not None
        session.add(
            StageRun(
                file_id="r1",
                stage=StageName.summarize,
                status=StageStatus.pending,
                detail={"stale": True},
            )
        )
        session.flush()
        assert sync_summary_document(session, current, settings) is None


def test_global_knowledge_sync_locks_library_then_sorted_recording_budgets(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "z-recording", "Z")
        _recording(session, "a-recording", "A")

    locks: list[str | None] = []
    real_lock = service.lock_cost_budget

    def observe_lock(session, file_id):
        locks.append(file_id)
        return real_lock(session, file_id)

    monkeypatch.setattr(service, "lock_cost_budget", observe_lock)
    with session_scope() as session:
        service.sync_knowledge_documents(session, settings)

    assert locks[:3] == [None, "a-recording", "z-recording"]


def test_note_edit_invalidates_old_chunks_and_failed_index_is_resumable(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import FileStatus, KnowledgeChunk, KnowledgeDocument, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        recording, _ = _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Version one", source_type="manual")
        session.add(note)
        session.flush()
        document = service.sync_user_note_document(session, note, settings)
        document_id, note_id = document.id, note.id

    monkeypatch.setattr(
        service,
        "_embed_note_chunks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert service.index_document(document_id, settings) is False
    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        assert document.status == "failed" and "offline" in document.error
        assert session.get(UserNote, note_id).content_md == "Version one"
        assert session.get(type(recording), "r1").status == FileStatus.done
        document.next_retry_at = None

    profile = {"stages": {"embed": {"connection": "embeddings:local"}}}
    monkeypatch.setattr(
        service,
        "_embed_note_chunks",
        lambda chunks, *_args: (
            [np.asarray([1.0, 0.0], dtype=np.float32).tobytes() for _ in chunks],
            "fake",
            2,
            profile,
            {},
        ),
    )
    assert service.index_document(document_id, settings) is True
    with session_scope() as session:
        note = session.get(UserNote, note_id)
        note.content_md = "Version two"
        note.version += 1
        service.sync_user_note_document(session, note, settings)
    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        assert document.status == "pending"
        assert (
            list(
                session.scalars(
                    select(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id)
                )
            )
            == []
        )


def test_old_note_index_publish_is_fenced_by_new_version(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import KnowledgeDocument, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Old body", source_type="manual")
        session.add(note)
        session.flush()
        document = service.sync_user_note_document(session, note, settings)
        document_id, note_id = document.id, note.id

    claim = service._claim_document(document_id, settings)
    with session_scope() as session:
        note = session.get(UserNote, note_id)
        note.content_md = "New body"
        note.version += 1
        service.sync_user_note_document(session, note, settings)
    assert (
        service._publish_document(
            claim,
            ["Plan\n\nOld body"],
            [np.asarray([1.0, 0.0], dtype=np.float32).tobytes()],
            "fake",
            2,
            {"stages": {"embed": {"connection": "embeddings:local"}}},
            {"usage": {"input_chars": 14}},
        )
        is False
    )
    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        assert document.status == "pending"
        assert document.artifact_version == 2


def test_embedding_profile_change_requeues_completed_note(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import KnowledgeDocument, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Current body", source_type="manual")
        session.add(note)
        session.flush()
        document = service.sync_user_note_document(session, note, settings)
        _complete_document(session, document, "Plan\n\nCurrent body")
        document_id, note_id = document.id, note.id

    changed_snapshot = {
        "stages": {
            "embed": {
                "connection": "embeddings:other",
                "model": "other-model",
                "provider_type": "openai",
                "execution_target": "cloud",
                "configuration": {},
                "options": {},
            }
        }
    }
    monkeypatch.setattr(service, "_resolved_embedding_snapshot", lambda *_args: changed_snapshot)
    with session_scope() as session:
        service.sync_user_note_document(session, session.get(UserNote, note_id), settings)
    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        assert document.status == "pending"
        assert document.profile_snapshot is None
        assert document.chunks == []


def test_missing_profile_snapshot_only_requeues_completed_note(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import KnowledgeChunk, KnowledgeDocument, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Current body", source_type="manual")
        session.add(note)
        session.flush()
        document = service.sync_user_note_document(session, note, settings)
        document_id, note_id = document.id, note.id
        pending_generation = document.generation

    with session_scope() as session:
        note = session.get(UserNote, note_id)
        document = service.sync_user_note_document(session, note, settings)
        assert document.status == "pending"
        assert document.profile_snapshot is None
        assert document.generation == pending_generation

        document.status = "completed"
        document.provider = "legacy"
        document.model = "legacy"
        document.dim = 2
        document.indexed_at = datetime.now(UTC)
        session.add(
            KnowledgeChunk(
                document_id=document.id,
                idx=0,
                text="Plan\n\nCurrent body",
                embedding_model="legacy",
                dim=2,
                embedding=np.asarray([1.0, 0.0], dtype=np.float32).tobytes(),
            )
        )

    with session_scope() as session:
        note = session.get(UserNote, note_id)
        service.sync_user_note_document(session, note, settings)

    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        assert document.status == "pending"
        assert document.profile_snapshot is None
        assert document.generation != pending_generation
        assert document.chunks == []


@pytest.mark.parametrize(
    ("connection_config", "expected_seconds"),
    [({}, 3900), ({"job_timeout": 5400}, 5700)],
)
def test_note_index_claim_lease_covers_remote_worker_timeout(
    monkeypatch, tmp_path, connection_config, expected_seconds
):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import ProviderConnection, UserNote
    from localplaud.db.session import session_scope

    remote = {
        "stages": {
            "embed": {
                "connection": "worker:gpu",
                "model": "remote-embed",
                "execution_target": "remote_worker",
            }
        },
        "policy": {},
    }

    class _Resolution:
        def to_dict(self):
            return remote

    monkeypatch.setattr(service, "resolve_recording_profile", lambda *_args: _Resolution())
    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        session.add(
            ProviderConnection(
                key="worker:gpu",
                name="Remote GPU",
                provider_type="localplaud-worker",
                execution_target="remote_worker",
                data_egress=True,
                config=connection_config,
            )
        )
        note = UserNote(file_id="r1", title="Plan", content_md="Current body", source_type="manual")
        session.add(note)
        session.flush()
        document_id = service.sync_user_note_document(session, note, settings).id

    before = datetime.now(UTC)
    claim = service._claim_document(document_id, settings)
    assert claim is not None
    with session_scope() as session:
        document = session.get(service.KnowledgeDocument, document_id)
        lease_until = service._as_utc(document.lease_until)
    assert lease_until >= before + timedelta(seconds=expected_seconds)
    assert lease_until < before + timedelta(seconds=expected_seconds + 60)


def test_note_index_claim_lease_covers_sequential_remote_fallbacks(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import ProviderConnection, UserNote
    from localplaud.db.session import session_scope

    resolved = {
        "stages": {
            "embed": {
                "connection": "worker:primary",
                "model": "primary",
                "execution_target": "remote_worker",
            }
        },
        "policy": {
            "fallback_policy": {
                "stages": {
                    "embed": [
                        {
                            "connection": "worker:fallback",
                            "model": "fallback",
                            "execution_target": "remote_worker",
                        }
                    ]
                }
            }
        },
    }

    class _Resolution:
        def to_dict(self):
            return resolved

    monkeypatch.setattr(service, "resolve_recording_profile", lambda *_args: _Resolution())
    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        session.add_all(
            [
                ProviderConnection(
                    key="worker:primary",
                    name="Primary worker",
                    provider_type="localplaud-worker",
                    execution_target="remote_worker",
                    data_egress=True,
                    config={"job_timeout": 1200},
                ),
                ProviderConnection(
                    key="worker:fallback",
                    name="Fallback worker",
                    provider_type="localplaud-worker",
                    execution_target="remote_worker",
                    data_egress=True,
                    config={"job_timeout": 2400},
                ),
            ]
        )
        note = UserNote(file_id="r1", title="Plan", content_md="Current body")
        session.add(note)
        session.flush()
        document_id = service.sync_user_note_document(session, note, settings).id

    before = datetime.now(UTC)
    assert service._claim_document(document_id, settings) is not None
    with session_scope() as session:
        lease_until = service._as_utc(
            session.get(service.KnowledgeDocument, document_id).lease_until
        )
    assert lease_until >= before + timedelta(seconds=4200)
    assert lease_until < before + timedelta(seconds=4260)


def test_note_index_claim_lease_covers_mixed_local_and_remote_fallbacks(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import ProviderConnection, UserNote
    from localplaud.db.session import session_scope

    resolved = {
        "stages": {
            "embed": {
                "connection": "embeddings:local",
                "model": "local",
                "execution_target": "local",
            }
        },
        "policy": {
            "fallback_policy": {
                "stages": {
                    "embed": [
                        {
                            "connection": "worker:fallback",
                            "model": "fallback",
                            "execution_target": "remote_worker",
                        }
                    ]
                }
            }
        },
    }

    class _Resolution:
        def to_dict(self):
            return resolved

    monkeypatch.setattr(service, "resolve_recording_profile", lambda *_args: _Resolution())
    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        session.add(
            ProviderConnection(
                key="worker:fallback",
                name="Fallback worker",
                provider_type="localplaud-worker",
                execution_target="remote_worker",
                data_egress=True,
                config={"job_timeout": 1200},
            )
        )
        note = UserNote(file_id="r1", title="Plan", content_md="Current body")
        session.add(note)
        session.flush()
        document_id = service.sync_user_note_document(session, note, settings).id

    before = datetime.now(UTC)
    assert service._claim_document(document_id, settings) is not None
    with session_scope() as session:
        lease_until = service._as_utc(
            session.get(service.KnowledgeDocument, document_id).lease_until
        )
    assert lease_until >= before + timedelta(seconds=3300)
    assert lease_until < before + timedelta(seconds=3360)


def test_local_note_index_claim_keeps_thirty_minute_lease(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Current body", source_type="manual")
        session.add(note)
        session.flush()
        document_id = service.sync_user_note_document(session, note, settings).id

    before = datetime.now(UTC)
    claim = service._claim_document(document_id, settings)
    assert claim is not None
    with session_scope() as session:
        document = session.get(service.KnowledgeDocument, document_id)
        lease_until = service._as_utc(document.lease_until)
    assert lease_until >= before + timedelta(minutes=30)
    assert lease_until < before + timedelta(minutes=31)


def test_stale_pending_id_does_not_reclaim_completed_document(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import KnowledgeDocument, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Current body", source_type="manual")
        session.add(note)
        session.flush()
        document = service.sync_user_note_document(session, note, settings)
        _complete_document(session, document, "Plan\n\nCurrent body")
        document_id = document.id

    assert service._claim_document(document_id, settings) is None
    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        assert document.status == "completed"
        assert document.attempts == 0
        assert len(document.chunks) == 1


def test_expired_note_index_takeover_closes_displaced_attempt(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import KnowledgeDocument, KnowledgeIndexAttempt, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Current body", source_type="manual")
        session.add(note)
        session.flush()
        document_id = service.sync_user_note_document(session, note, settings).id

    first = service._claim_document(document_id, settings)
    assert first is not None
    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        document.lease_until = datetime.now(UTC) - timedelta(seconds=1)

    second = service._claim_document(document_id, settings)
    assert second is not None
    assert second["attempt_id"] != first["attempt_id"]
    with session_scope() as session:
        attempts = list(
            session.scalars(
                select(KnowledgeIndexAttempt)
                .where(KnowledgeIndexAttempt.document_id == document_id)
                .order_by(KnowledgeIndexAttempt.attempt)
            )
        )
        document = session.get(KnowledgeDocument, document_id)
        assert [attempt.status for attempt in attempts] == ["skipped", "running"]
        assert attempts[0].completed_at is not None
        assert "lease expired" in attempts[0].error
        assert document.lease_token == second["lease_token"]


def test_expired_note_index_claim_never_dispatches_to_provider(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import KnowledgeDocument, KnowledgeIndexAttempt, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Current body")
        session.add(note)
        session.flush()
        document_id = service.sync_user_note_document(session, note, settings).id

    claim = service._claim_document(document_id, settings)
    assert claim is not None
    with session_scope() as session:
        session.get(KnowledgeDocument, document_id).lease_until = datetime.now(UTC) - timedelta(
            seconds=1
        )

    monkeypatch.setattr(
        service,
        "build_embedder",
        lambda *_args, **_kwargs: pytest.fail("expired claim must not reach provider"),
    )
    with pytest.raises(RuntimeError, match="superseded before provider dispatch"):
        service._embed_note_chunks(["Plan\n\nCurrent body"], settings, claim["snapshot"], claim)

    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        attempt = session.get(KnowledgeIndexAttempt, claim["attempt_id"])
        assert document.status == "pending"
        assert document.lease_token is None and document.lease_until is None
        assert attempt.status == "skipped"
        assert attempt.completed_at is not None


def test_expired_note_index_claim_cannot_publish(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import KnowledgeDocument, KnowledgeIndexAttempt, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Current body")
        session.add(note)
        session.flush()
        document_id = service.sync_user_note_document(session, note, settings).id

    claim = service._claim_document(document_id, settings)
    assert claim is not None
    with session_scope() as session:
        session.get(KnowledgeDocument, document_id).lease_until = datetime.now(UTC) - timedelta(
            seconds=1
        )

    assert (
        service._publish_document(
            claim,
            ["Plan\n\nCurrent body"],
            [np.asarray([1.0], dtype=np.float32).tobytes()],
            "fake",
            1,
            claim["snapshot"],
            {"usage": {}},
        )
        is False
    )
    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        attempt = session.get(KnowledgeIndexAttempt, claim["attempt_id"])
        assert document.status == "pending" and document.chunks == []
        assert attempt.status == "skipped"


@pytest.mark.parametrize("supersession", ["edited", "deleted"])
def test_superseded_note_is_never_dispatched_to_embedding_provider(
    monkeypatch, tmp_path, supersession
):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import KnowledgeIndexAttempt, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Current body", source_type="manual")
        session.add(note)
        session.flush()
        document = service.sync_user_note_document(session, note, settings)
        document_id, note_id = document.id, note.id

    claim = service._claim_document(document_id, settings)
    assert claim is not None
    with session_scope() as session:
        if supersession == "edited":
            note = session.get(UserNote, note_id)
            note.content_md = "Updated body"
            note.version += 1
            service.sync_user_note_document(session, note, settings)
        else:
            document = session.get(service.KnowledgeDocument, document_id)
            service._delete_document(session, document)

    calls: list[str] = []

    def unexpected_call(*_args, **_kwargs):
        calls.append("provider")
        raise AssertionError("superseded content reached a provider boundary")

    monkeypatch.setattr(service, "_embedding_cost_guard", unexpected_call)
    monkeypatch.setattr(service, "build_embedder", unexpected_call)
    with pytest.raises(RuntimeError, match="superseded before provider dispatch"):
        service._embed_note_chunks(["Plan\n\nCurrent body"], settings, claim["snapshot"], claim)
    assert calls == []
    with session_scope() as session:
        attempt = session.get(KnowledgeIndexAttempt, claim["attempt_id"])
        assert attempt.status == "skipped"
        assert attempt.completed_at is not None


def test_fallback_index_identity_stays_current_without_reembedding(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope
    from localplaud.providers.fallback import candidate_snapshots

    current = {
        "stages": {
            "embed": {
                "connection": "embeddings:primary",
                "model": "primary",
                "execution_target": "cloud",
            }
        },
        "policy": {
            "fallback_policy": {
                "stages": {
                    "embed": [
                        {
                            "connection": "embeddings:fallback",
                            "model": "fallback",
                            "execution_target": "cloud",
                        }
                    ]
                }
            }
        },
    }
    fallback = candidate_snapshots(current, "embed")[1]
    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Current body", source_type="manual")
        session.add(note)
        session.flush()
        document = service.sync_user_note_document(session, note, settings)
        _complete_document(session, document, "Plan\n\nCurrent body")
        document.profile_snapshot = fallback
        document_id, note_id, generation = document.id, note.id, document.generation

    monkeypatch.setattr(service, "_resolved_embedding_snapshot", lambda *_args: current)
    with session_scope() as session:
        service.sync_user_note_document(session, session.get(UserNote, note_id), settings)
    with session_scope() as session:
        document = session.get(service.KnowledgeDocument, document_id)
        assert document.status == "completed"
        assert document.generation == generation
        assert len(document.chunks) == 1


def test_running_note_index_is_fenced_when_profile_changes(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Current body", source_type="manual")
        session.add(note)
        session.flush()
        document = service.sync_user_note_document(session, note, settings)
        document_id, note_id = document.id, note.id
    claim = service._claim_document(document_id, settings)
    changed = {
        "stages": {
            "embed": {
                "connection": "embeddings:new",
                "model": "new-model",
                "execution_target": "cloud",
            }
        },
        "policy": {},
    }
    monkeypatch.setattr(service, "_resolved_embedding_snapshot", lambda *_args: changed)
    with session_scope() as session:
        service.sync_user_note_document(session, session.get(UserNote, note_id), settings)
        document = session.get(service.KnowledgeDocument, document_id)
        assert document.status == "pending"
        assert document.generation != claim["generation"]
    assert (
        service._publish_document(
            claim,
            ["Plan\n\nCurrent body"],
            [np.asarray([1.0, 0.0], dtype=np.float32).tobytes()],
            "old-model",
            2,
            claim["snapshot"],
            {"usage": {}},
        )
        is False
    )


def test_running_summarize_document_can_claim_and_publish(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import StageName, StageRun, StageStatus, Summary
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _, transcript = _recording(session, "r1", "Weekly Sync")
        summary = Summary(
            file_id="r1",
            template="meeting",
            title="Decisions",
            content_md="Ship Friday",
            source="local",
            input_transcript_id=transcript.id,
            input_transcript_revision=0,
            input_transcript_source="local",
        )
        session.add_all(
            [
                summary,
                StageRun(
                    file_id="r1",
                    stage=StageName.summarize,
                    status=StageStatus.running,
                    detail={"stale": True},
                ),
            ]
        )
        session.flush()
        document = service.sync_summary_document(
            session, summary, settings, allow_running_stage=True
        )
        document_id = document.id
    claim = service._claim_document(document_id, settings)
    assert claim is not None
    assert (
        service._publish_document(
            claim,
            ["Decisions\n\nShip Friday"],
            [np.asarray([1.0, 0.0], dtype=np.float32).tobytes()],
            "fake",
            2,
            claim["snapshot"],
            {"usage": {}},
        )
        is True
    )
    with session_scope() as session:
        assert session.get(service.KnowledgeDocument, document_id).status == "completed"


def test_transcript_reindex_uses_profile_fallback_and_persists_identity(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.reindex as reindex_service
    from localplaud.db.models import Chunk, StageAttempt, StageName, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.embeddings.base import EmbeddingUnavailable

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
    resolved = {
        "stages": {
            "embed": {
                "connection": "embeddings:primary",
                "model": "primary",
                "execution_target": "local",
            }
        },
        "policy": {
            "fallback_policy": {
                "stages": {
                    "embed": [
                        {
                            "connection": "embeddings:fallback",
                            "model": "fallback",
                            "execution_target": "local",
                        }
                    ]
                }
            }
        },
    }

    class Resolved:
        def to_dict(self):
            return resolved

    calls = 0

    def fake_embed(_chunks, _settings):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise EmbeddingUnavailable("primary unavailable")
        return [np.asarray([1.0], dtype=np.float32).tobytes()], "fallback", 1

    monkeypatch.setattr(reindex_service, "resolve_recording_profile", lambda *_args: Resolved())
    monkeypatch.setattr(reindex_service, "_settings_for_stage", lambda current, *_args: current)
    monkeypatch.setattr(reindex_service, "_cost_guard", lambda *_args: {})
    monkeypatch.setattr(reindex_service.index, "embed_chunks", fake_embed)
    assert reindex_service.reindex_file("r1", settings) is True
    with session_scope() as session:
        chunk = session.scalar(select(Chunk).where(Chunk.file_id == "r1"))
        attempts = list(
            session.scalars(
                select(StageAttempt)
                .where(
                    StageAttempt.file_id == "r1",
                    StageAttempt.stage == StageName.index,
                )
                .order_by(StageAttempt.attempt)
            )
        )
        assert chunk.resolved_profile_snapshot["stages"]["embed"]["model"] == "fallback"
        assert [attempt.status for attempt in attempts] == [
            StageStatus.failed,
            StageStatus.completed,
        ]


def test_profile_change_fences_running_transcript_index_attempt(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import (
        Chunk,
        StageAttempt,
        StageName,
        StageRun,
        StageStatus,
    )
    from localplaud.db.session import session_scope

    old = {
        "stages": {
            "embed": {
                "connection": "embeddings:old",
                "model": "old",
                "execution_target": "local",
            }
        }
    }
    new = {
        "stages": {
            "embed": {
                "connection": "embeddings:new",
                "model": "new",
                "execution_target": "local",
            }
        }
    }
    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        session.add(
            Chunk(
                file_id="r1",
                idx=0,
                text="old index",
                dim=1,
                embedding=np.asarray([1.0], dtype=np.float32).tobytes(),
                resolved_profile_snapshot=old,
            )
        )
        session.add(
            StageRun(
                file_id="r1",
                stage=StageName.index,
                status=StageStatus.running,
                attempts=1,
                detail={"stale_generation": "old-generation"},
            )
        )
        session.add(
            StageAttempt(
                file_id="r1",
                stage=StageName.index,
                attempt=1,
                status=StageStatus.running,
                usage={"projection": True},
                estimated_cost_usd=0.03,
            )
        )
    monkeypatch.setattr(service, "_resolved_embedding_snapshot", lambda *_args: new)
    with session_scope() as session:
        assert service.sync_transcript_index_profiles(
            session, file_ids=["r1"], settings=settings
        ) == ["r1"]
    with session_scope() as session:
        run = session.scalar(
            select(StageRun).where(StageRun.file_id == "r1", StageRun.stage == StageName.index)
        )
        attempt = session.scalar(select(StageAttempt).where(StageAttempt.file_id == "r1"))
        assert run.status == StageStatus.pending
        assert run.detail["stale_generation"] != "old-generation"
        assert attempt.status == StageStatus.skipped
        assert attempt.estimated_cost_usd == pytest.approx(0.03)
        assert session.query(Chunk).filter_by(file_id="r1").count() == 0


def test_retrieval_excludes_same_dimension_foreign_embedding_space(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Current body", source_type="manual")
        session.add(note)
        session.flush()
        document = service.sync_user_note_document(session, note, settings)
        _complete_document(session, document, "Plan\n\nCurrent body")
        document.profile_snapshot = {
            "stages": {
                "embed": {
                    "connection": "embeddings:foreign",
                    "model": "same-dim-other-space",
                    "execution_target": "cloud",
                }
            }
        }
    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda _settings: _QueryEmbedder())
    from localplaud.worker.qa import retrieve

    assert retrieve("Current", file_id="r1", settings=settings) == []


def test_note_index_attempts_reserve_cumulative_recording_cost(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.providers.usage as usage_service
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope
    from localplaud.providers.usage import CostPolicyError

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        notes = [
            UserNote(
                file_id="r1",
                title=f"Plan {index}",
                content_md="Body",
                source_type="manual",
            )
            for index in range(2)
        ]
        session.add_all(notes)
        session.flush()
        document_ids = [
            service.sync_user_note_document(session, note, settings).id for note in notes
        ]
    claims = [service._claim_document(document_id, settings) for document_id in document_ids]
    priced = {
        "stages": {
            "embed": {
                "connection": "embeddings:cloud",
                "model": "priced",
                "execution_target": "cloud",
            }
        },
        "policy": {"cost_ceiling": 0.10},
    }
    monkeypatch.setattr(
        service,
        "resolve_recording_profile",
        lambda *_args, **_kwargs: SimpleNamespace(to_dict=lambda: priced),
    )
    monkeypatch.setattr(
        usage_service, "pricing_for_stage", lambda *_args: {"per_request_usd": 0.06}
    )
    with session_scope() as session:
        first = service._embedding_cost_guard(session, priced, 10, claims[0])
    assert first["reserved_cost_usd"] == 0.06
    with session_scope() as session:
        with pytest.raises(CostPolicyError, match="cumulative"):
            service._embedding_cost_guard(session, priced, 10, claims[1])


def test_zero_cost_note_dispatch_has_owner_lease_and_fingerprint(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import ProviderCostReservation, UserNote
    from localplaud.db.session import session_scope
    from localplaud.providers.usage import provider_dispatch_fingerprint

    external = {
        "stages": {
            "embed": {
                "connection": "embeddings:free-cloud",
                "model": "free",
                "execution_target": "cloud",
            }
        },
        "policy": {"no_egress": False, "cost_ceiling": None},
    }
    with session_scope() as session:
        _recording(session, "zero-note", "Zero note")
        note = UserNote(
            file_id="zero-note", title="Note", content_md="Body", source_type="manual"
        )
        session.add(note)
        session.flush()
        document_id = service.sync_user_note_document(session, note, settings).id
    claim = service._claim_document(document_id, settings)
    monkeypatch.setattr(
        service,
        "resolve_recording_profile",
        lambda *_args, **_kwargs: SimpleNamespace(to_dict=lambda: external),
    )
    with session_scope() as session:
        cost = service._embedding_cost_guard(session, external, 10, claim)
        row = session.get(ProviderCostReservation, cost["reservation_id"])
        assert row.status == "active"
        assert row.owner
        assert row.lease_until is not None
        assert row.profile_fingerprint == provider_dispatch_fingerprint(external, "embed")


def test_note_takeover_waits_for_dispatch_then_settles_displaced_cost(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import (
        KnowledgeIndexAttempt,
        ProviderCostReservation,
        UserNote,
    )
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "takeover", "Takeover")
        note = UserNote(
            file_id="takeover", title="Plan", content_md="Body", source_type="manual"
        )
        session.add(note)
        session.flush()
        document_id = service.sync_user_note_document(session, note, settings).id
    first = service._claim_document(document_id, settings)
    reservation_id = "note:takeover:embed"
    with session_scope() as session:
        document = session.get(service.KnowledgeDocument, document_id)
        document.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        attempt = session.get(KnowledgeIndexAttempt, first["attempt_id"])
        attempt.usage = {"dispatch_reservation_ids": [reservation_id]}
        session.add(
            ProviderCostReservation(
                id=reservation_id,
                scope_key="file:takeover",
                file_id="takeover",
                operation="embed",
                status="active",
                lease_until=datetime.now(UTC) + timedelta(minutes=5),
                estimated_cost_usd=0.5,
            )
        )

    assert service._claim_document(document_id, settings) is None
    with session_scope() as session:
        reservation = session.get(ProviderCostReservation, reservation_id)
        reservation.lease_until = datetime.now(UTC) - timedelta(seconds=1)

    replacement = service._claim_document(document_id, settings)
    assert replacement is not None and replacement["lease_token"] != first["lease_token"]
    with session_scope() as session:
        displaced = session.get(KnowledgeIndexAttempt, first["attempt_id"])
        assert displaced.status == "skipped"
        assert displaced.estimated_cost_usd == 0.5
        assert session.get(ProviderCostReservation, reservation_id) is None


def test_expired_note_owner_cannot_write_document_failure(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import KnowledgeDocument, KnowledgeIndexAttempt, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "expired-owner", "Expired owner")
        note = UserNote(
            file_id="expired-owner", title="Plan", content_md="Body", source_type="manual"
        )
        session.add(note)
        session.flush()
        document_id = service.sync_user_note_document(session, note, settings).id
    claim = service._claim_document(document_id, settings)
    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        document.lease_until = datetime.now(UTC) - timedelta(seconds=1)

    service._fail_document(claim, RuntimeError("late failure"))
    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        attempt = session.get(KnowledgeIndexAttempt, claim["attempt_id"])
        assert document.status == "running"
        assert document.error is None
        assert document.lease_token == claim["lease_token"]
        assert attempt.status == "failed"


def test_note_index_and_profile_mutation_are_fenced_in_both_orderings(
    monkeypatch, tmp_path
):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import ExecutionProfile, KnowledgeDocument, UserNote
    from localplaud.db.session import session_scope
    from localplaud.providers.service import (
        ProfileMutationBusyError,
        lock_recording_profile_change,
    )

    with session_scope() as session:
        _recording(session, "note-race", "Note race")
        note = UserNote(
            file_id="note-race", title="Note", content_md="Body", source_type="manual"
        )
        session.add(note)
        session.flush()
        document_id = service.sync_user_note_document(session, note, settings).id
    claim = service._claim_document(document_id, settings)

    with session_scope() as session:
        with pytest.raises(ProfileMutationBusyError, match="note indexing"):
            lock_recording_profile_change(session, "note-race")

    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        document.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        lock_recording_profile_change(session, "note-race")
        profile = session.query(ExecutionProfile).filter_by(is_system_default=True).one()
        profile.cost_ceiling = 321.0

    provider_calls = 0

    def provider(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return _QueryEmbedder()

    monkeypatch.setattr(service, "build_embedder", provider)
    chunks = service.build_note_chunks(claim["title"], claim["content"])
    with pytest.raises(RuntimeError, match="superseded before provider dispatch"):
        service._embed_note_chunks(chunks, settings, claim["snapshot"], claim)
    assert provider_calls == 0


def test_note_and_pipeline_attempts_share_one_cost_ceiling(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.providers.usage as usage_service
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import StageAttempt, StageName, StageStatus, UserNote
    from localplaud.db.session import session_scope
    from localplaud.providers.usage import CostPolicyError, enforce_cost_ceiling

    priced = {
        "stages": {
            "embed": {
                "connection": "embeddings:cloud",
                "model": "priced",
                "execution_target": "cloud",
            }
        },
        "policy": {"cost_ceiling": 0.10},
    }
    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(file_id="r1", title="Plan", content_md="Body", source_type="manual")
        session.add(note)
        session.flush()
        document_id = service.sync_user_note_document(session, note, settings).id
    claim = service._claim_document(document_id, settings)
    monkeypatch.setattr(
        service,
        "resolve_recording_profile",
        lambda *_args, **_kwargs: SimpleNamespace(to_dict=lambda: priced),
    )
    monkeypatch.setattr(
        usage_service, "pricing_for_stage", lambda *_args: {"per_request_usd": 0.06}
    )
    with session_scope() as session:
        service._embedding_cost_guard(session, priced, 10, claim)
    with session_scope() as session:
        session.add(
            StageAttempt(
                file_id="r1",
                stage=StageName.index,
                attempt=1,
                status=StageStatus.running,
                resolved_profile_snapshot=priced,
                usage={},
            )
        )
    with session_scope() as session:
        with pytest.raises(CostPolicyError, match="recording ceiling"):
            enforce_cost_ceiling(
                session,
                "r1",
                "embed",
                priced,
                {"input_chars": 10, "requests": 1},
            )


def test_library_note_attempts_share_one_global_cost_ceiling(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.providers.usage as usage_service
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope
    from localplaud.providers.usage import CostPolicyError

    with session_scope() as session:
        notes = [
            UserNote(
                file_id=None,
                title=f"Library {index}",
                content_md="Body",
                source_type="manual",
            )
            for index in range(2)
        ]
        session.add_all(notes)
        session.flush()
        document_ids = [
            service.sync_user_note_document(session, note, settings).id for note in notes
        ]
    claims = [service._claim_document(document_id, settings) for document_id in document_ids]
    priced = {
        "stages": {
            "embed": {
                "connection": "embeddings:cloud",
                "model": "priced",
                "execution_target": "cloud",
            }
        },
        "policy": {"cost_ceiling": 0.10},
    }
    monkeypatch.setattr(
        service,
        "preview_resolution",
        lambda *_args, **_kwargs: SimpleNamespace(to_dict=lambda: priced),
    )
    monkeypatch.setattr(
        usage_service, "pricing_for_stage", lambda *_args: {"per_request_usd": 0.06}
    )
    with session_scope() as session:
        service._embedding_cost_guard(session, priced, 10, claims[0])
    with session_scope() as session:
        with pytest.raises(CostPolicyError, match="cumulative"):
            service._embedding_cost_guard(session, priced, 10, claims[1])


def test_bulk_generated_invalidation_removes_chunks_with_foreign_keys_off(monkeypatch, tmp_path):
    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.db.models import KnowledgeChunk, Summary
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _, transcript = _recording(session, "r1", "Weekly Sync")
        summary = Summary(
            file_id="r1",
            template="meeting",
            content_md="Private note body",
            source="local",
            input_transcript_id=transcript.id,
            input_transcript_revision=0,
            input_transcript_source="local",
        )
        session.add(summary)
        session.flush()
        document = service.sync_summary_document(session, summary, settings)
        session.add(
            KnowledgeChunk(
                document_id=document.id,
                idx=0,
                text="Private note body",
                embedding_model="fake",
                dim=1,
                embedding=np.asarray([1.0], dtype=np.float32).tobytes(),
            )
        )
        service.invalidate_generated_documents(session, "r1")
    with session_scope() as session:
        assert session.query(KnowledgeChunk).count() == 0


def test_saved_note_api_queues_invalidates_and_deletes_document(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    from localplaud.api.app import app
    from localplaud.db.models import KnowledgeChunk, KnowledgeDocument, UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
    client = TestClient(app)
    created = client.post(
        "/api/files/r1/notes",
        json={"title": "Plan", "content_md": "Version one"},
    )
    assert created.status_code == 201
    note_id = created.json()["id"]
    with session_scope() as session:
        document = session.scalar(
            select(KnowledgeDocument).where(KnowledgeDocument.user_note_id == note_id)
        )
        assert document.status == "pending" and document.artifact_version == 1
        session.add(
            KnowledgeChunk(
                document_id=document.id,
                idx=0,
                text="stale",
                embedding_model="fake",
                dim=1,
                embedding=np.asarray([1.0], dtype=np.float32).tobytes(),
            )
        )
        document.status = "completed"
        document_id = document.id

    lock_order: list[str] = []
    real_budget = service.lock_cost_budget
    real_artifact = service._lock_artifact
    real_document = service._lock_document

    def observe_budget(*args, **kwargs):
        lock_order.append("budget")
        return real_budget(*args, **kwargs)

    def observe_artifact(*args, **kwargs):
        lock_order.append("artifact")
        return real_artifact(*args, **kwargs)

    def observe_document(*args, **kwargs):
        lock_order.append("document")
        return real_document(*args, **kwargs)

    monkeypatch.setattr(service, "lock_cost_budget", observe_budget)
    monkeypatch.setattr(service, "_lock_artifact", observe_artifact)
    monkeypatch.setattr(service, "_lock_document", observe_document)

    updated = client.put(
        f"/api/notes/{note_id}",
        json={"title": "Plan", "content_md": "Version two", "base_version": 1},
    )
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert lock_order[:3] == ["budget", "artifact", "document"]
    with session_scope() as session:
        document = session.get(KnowledgeDocument, document_id)
        assert document.status == "pending" and document.artifact_version == 2
        assert document.chunks == []

    lock_order.clear()
    assert client.delete(f"/api/notes/{note_id}").status_code == 204
    assert lock_order[:3] == ["budget", "artifact", "document"]
    with session_scope() as session:
        assert session.get(UserNote, note_id) is None
        assert session.get(KnowledgeDocument, document_id) is None


def test_active_library_ask_lease_blocks_note_evidence_mutation(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    _database(monkeypatch, tmp_path)
    from localplaud.api.app import app
    from localplaud.db.models import AskThread, Summary, UserNote
    from localplaud.db.session import session_scope
    from localplaud.worker.knowledge_index import (
        KnowledgeIndexBusyError,
        lock_summary_for_mutation,
    )

    with session_scope() as session:
        _, transcript = _recording(session, "ask-evidence", "Ask evidence")
        note = UserNote(
            file_id="ask-evidence",
            title="Plan",
            content_md="Private plan",
            source_type="manual",
        )
        summary = Summary(
            file_id="ask-evidence",
            template="meeting",
            title="Meeting",
            content_md="Generated plan",
            source="local",
            input_transcript_id=transcript.id,
            input_transcript_revision=0,
            input_transcript_source="local",
        )
        session.add_all([note, summary])
        session.flush()
        note_id, summary_id = note.id, summary.id
        session.add(
            AskThread(
                id="active-library-ask",
                file_id=None,
                title="Active",
                request_token="ask-token",
                request_lease_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        )

    client = TestClient(app)
    edited = client.put(
        f"/api/notes/{note_id}",
        json={"title": "Changed", "content_md": "Changed", "base_version": 1},
    )
    assert edited.status_code == 409
    assert "used by Ask" in edited.json()["detail"]
    with session_scope() as session:
        with pytest.raises(KnowledgeIndexBusyError, match="used by Ask"):
            lock_summary_for_mutation(session, summary_id, "ask-evidence")


def test_note_version_permalink_survives_live_edit(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    _database(monkeypatch, tmp_path)
    from localplaud.api.app import app
    from localplaud.db.models import UserNote
    from localplaud.db.session import session_scope

    with session_scope() as session:
        _recording(session, "r1", "Weekly Sync")
        note = UserNote(
            file_id="r1", title="Plan", content_md="Original cited body", source_type="manual"
        )
        session.add(note)
        session.flush()
        note_id = note.id
    client = TestClient(app)
    assert (
        client.put(
            f"/api/notes/{note_id}",
            json={"title": "Plan", "content_md": "New live body", "base_version": 1},
        ).status_code
        == 200
    )
    archived = client.get(f"/notes/{note_id}/versions/1")
    assert archived.status_code == 200
    assert "Original cited body" in archived.text
    assert "New live body" not in archived.text
    assert "Historical version" in archived.text


def test_generated_note_permalink_survives_regeneration(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    _database(monkeypatch, tmp_path)
    from localplaud.api.app import app
    from localplaud.db.models import Summary
    from localplaud.db.session import session_scope
    from localplaud.note_history import archive_summary

    with session_scope() as session:
        _, transcript = _recording(session, "r1", "Weekly Sync")
        original = Summary(
            file_id="r1",
            template="meeting",
            title="Decisions",
            content_md="Original generated body",
            source="local",
            input_transcript_id=transcript.id,
            input_transcript_revision=0,
            input_transcript_source="local",
        )
        session.add(original)
        session.flush()
        archive_summary(session, original, reason="regenerated")
        session.delete(original)
        session.flush()
        session.add(
            Summary(
                file_id="r1",
                template="meeting",
                title="Decisions",
                content_md="New generated body",
                source="local",
                input_transcript_id=transcript.id,
                input_transcript_revision=0,
                input_transcript_source="local",
            )
        )
    client = TestClient(app)
    archived = client.get("/file/r1/notes/generated/meeting/versions/1")
    assert archived.status_code == 200
    assert "Original generated body" in archived.text
    assert "New generated body" not in archived.text


def test_remote_note_embedding_preserves_chunk_contract(monkeypatch, tmp_path):
    import base64

    settings = _database(monkeypatch, tmp_path)
    import localplaud.worker.knowledge_index as service
    import localplaud.worker.pipeline as pipeline

    snapshot = {
        "stages": {
            "embed": {
                "connection": "worker:gpu",
                "model": "remote-embed",
                "execution_target": "remote_worker",
            }
        },
        "policy": {},
    }
    monkeypatch.setattr(service, "candidate_snapshots", lambda *_args: [snapshot])
    monkeypatch.setattr(service, "_revalidate_claim_for_dispatch", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        service,
        "_embedding_cost_guard",
        lambda *_args: {"estimated_cost_usd": 0},
    )

    def fake_remote(file_id, candidate, stage, inputs):
        assert file_id == "knowledge-document-9"
        assert candidate is snapshot and stage == "embed"
        texts = [segment["text"] for segment in inputs[0].value["segments"]]
        return {
            "chunks": [{"text": text} for text in texts],
            "vectors_base64": [
                base64.b64encode(np.asarray([1.0, 0.0], dtype=np.float32).tobytes()).decode()
                for _ in texts
            ],
            "model": "remote-embed",
            "dim": 2,
        }

    monkeypatch.setattr(pipeline, "_run_remote_stage", fake_remote)
    blobs, model, dim, profile, _cost = service._embed_note_chunks(
        ["First", "Second"], settings, snapshot, {"id": 9, "attempt_id": 1}
    )
    assert len(blobs) == 2 and model == "remote-embed" and dim == 2
    assert profile is snapshot

    monkeypatch.setattr(
        pipeline,
        "_run_remote_stage",
        lambda *_args, **_kwargs: {
            "chunks": [{"text": "First"}, {"text": "Second"}],
            "vectors_base64": [
                base64.b64encode(np.asarray([1.0, 0.0], dtype=np.float32).tobytes()).decode(),
                base64.b64encode(np.asarray([0.0, 1.0], dtype=np.float32).tobytes()).decode(),
            ],
            "model": "wrong-space",
            "dim": 2,
        },
    )
    with pytest.raises(ValueError, match="different model"):
        service._embed_note_chunks(
            ["First", "Second"], settings, snapshot, {"id": 9, "attempt_id": 1}
        )

    monkeypatch.setattr(
        pipeline,
        "_run_remote_stage",
        lambda *_args, **_kwargs: {
            "chunks": [{"text": "First"}, {"text": "Second"}],
            "vectors_base64": [
                base64.b64encode(np.asarray([1.0, 0.0], dtype=np.float32).tobytes()).decode(),
                base64.b64encode(np.asarray([0.0, 1.0], dtype=np.float32).tobytes()).decode(),
            ],
            "dim": 2,
        },
    )
    with pytest.raises(ValueError, match="returned no model"):
        service._embed_note_chunks(
            ["First", "Second"], settings, snapshot, {"id": 9, "attempt_id": 1}
        )

    monkeypatch.setattr(
        pipeline,
        "_run_remote_stage",
        lambda *_args, **_kwargs: {
            "chunks": [{"text": "First"}, {"text": "Second"}],
            "vectors_base64": ["not-base64", "also-bad"],
            "model": "remote-embed",
            "dim": 2,
        },
    )
    with pytest.raises(Exception, match="base64|vector"):
        service._embed_note_chunks(
            ["First", "Second"], settings, snapshot, {"id": 9, "attempt_id": 1}
        )
