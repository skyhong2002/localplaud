"""Tests for single-file Ask: file-scoped retrieval, answer shape, and the
POST /file/{id}/ask web fragment with playable timestamp citations."""

from __future__ import annotations

import base64
import re

import numpy as np
import pytest


def _fresh_db(monkeypatch, tmp_path, name="qa.db"):
    """Point the app at an isolated SQLite DB and reset the engine cache."""
    import localplaud.db.session as db_session
    from localplaud.config import get_settings
    from localplaud.db.session import init_db

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / name}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    init_db()


class _FakeEmbedder:
    """Returns a fixed 2-d query vector aligned with the [1, 0] axis."""

    name = "fake"
    dim = 2

    def available(self):
        return True

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


class _FakeLlm:
    name = "fake"

    def available(self):
        return True

    def complete(self, prompt, system=None, temperature=0.3, max_tokens=2048):
        return "Grounded answer."


def _seed_two_files(profile_snapshot=None):
    from localplaud.db.models import Chunk, PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.providers.service import resolve_recording_profile

    hit = np.array([1.0, 0.0], dtype=np.float32)  # aligns with the query
    miss = np.array([0.0, 1.0], dtype=np.float32)  # orthogonal
    with session_scope() as s:
        s.add(PlaudFile(id="r1", filename="Recording One"))
        s.add(PlaudFile(id="r2", filename="Recording Two"))
        s.flush()
        snapshot = profile_snapshot or resolve_recording_profile(s, "r1").to_dict()
        s.add(
            Chunk(
                file_id="r1",
                idx=0,
                text="r1 relevant",
                start=12.0,
                end=15.0,
                speaker="SPEAKER_00",
                embedding=hit.tobytes(),
                dim=2,
                resolved_profile_snapshot=snapshot,
            )
        )
        s.add(
            Chunk(
                file_id="r1",
                idx=1,
                text="r1 offtopic",
                start=40.0,
                end=42.0,
                embedding=miss.tobytes(),
                dim=2,
                resolved_profile_snapshot=snapshot,
            )
        )
        s.add(
            Chunk(
                file_id="r2",
                idx=0,
                text="r2 relevant",
                start=3.0,
                end=6.0,
                embedding=hit.tobytes(),
                dim=2,
                resolved_profile_snapshot=snapshot,
            )
        )


def test_retrieve_scopes_to_file(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _seed_two_files()
    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda cfg: _FakeEmbedder())
    from localplaud.db.models import Chunk, Speaker
    from localplaud.db.session import session_scope
    from localplaud.worker.qa import retrieve

    with session_scope() as session:
        session.add(Speaker(file_id="r1", key="SPEAKER_00", display_name="Sky"))

    # Unscoped: both files' relevant chunks surface.
    all_hits = retrieve("q", top_k=6)
    assert {h["file_id"] for h in all_hits} == {"r1", "r2"}

    # Scoped: only the requested recording's chunks are returned.
    scoped = retrieve("q", top_k=6, file_id="r1")
    assert scoped
    assert all(h["file_id"] == "r1" for h in scoped)
    assert scoped[0]["text"] == "r1 relevant"
    assert scoped[0]["speaker"] == "Sky"
    assert scoped[0]["speaker_key"] == "SPEAKER_00"
    with session_scope() as session:
        assert session.query(Chunk).filter_by(file_id="r1", idx=0).one().speaker == "SPEAKER_00"
        session.query(Chunk).filter_by(file_id="r1", idx=0).one().speaker = "Sky"
    legacy = retrieve(
        "q", top_k=1, file_id="r1", include_evidence_fingerprints=True
    )[0]
    assert legacy["speaker"] == "Sky"
    assert legacy["speaker_key"] == "SPEAKER_00"
    assert legacy["_evidence_fingerprint"]["speaker_key"] == "SPEAKER_00"
    from localplaud.worker.qa import _format_context

    assert "[Recording One @ 12s · Sky]" in _format_context([legacy])
    with session_scope() as session:
        session.add(Speaker(file_id="r1", key="SPEAKER_01", display_name="Sky"))
    ambiguous = retrieve("q", top_k=1, file_id="r1")[0]
    assert ambiguous["speaker"] == "Sky"
    assert ambiguous["speaker_key"] is None


def test_mutation_first_stale_ask_fingerprint_blocks_provider_call(
    monkeypatch, tmp_path
):
    _fresh_db(monkeypatch, tmp_path)
    _seed_two_files()
    from localplaud.db.models import ExecutionProfile
    from localplaud.db.session import session_scope
    from localplaud.providers.service import (
        lock_library_profile_change,
        resolve_recording_profile,
    )
    from localplaud.worker.qa import _candidate_cost

    with session_scope() as session:
        stale = resolve_recording_profile(session, "r1").to_dict()
        lock_library_profile_change(session)
        profile = session.query(ExecutionProfile).filter_by(is_system_default=True).one()
        profile.cost_ceiling = 123.0

    provider_calls = 0

    def dispatch():
        nonlocal provider_calls
        _candidate_cost(
            stale,
            "ask",
            {"input_chars": 10, "requests": 1},
            0.0,
            "mutation-first:ask",
            "r1",
        )
        provider_calls += 1

    with pytest.raises(RuntimeError, match="profile changed before dispatch"):
        dispatch()
    assert provider_calls == 0


def test_note_artifact_locks_use_deterministic_postgresql_id_order():
    from sqlalchemy.dialects import postgresql

    from localplaud.db.models import Summary, UserNote
    from localplaud.worker.qa import _ordered_artifact_lock_query

    for model, table in ((Summary, "summaries"), (UserNote, "user_notes")):
        compiled = str(
            _ordered_artifact_lock_query(model, [9, 2, 5]).compile(dialect=postgresql.dialect())
        )
        assert f"ORDER BY {table}.id" in compiled
        assert "FOR UPDATE" in compiled


def test_transcript_revision_between_retrieval_and_llm_egress_blocks_provider(
    monkeypatch, tmp_path
):
    _fresh_db(monkeypatch, tmp_path)
    import localplaud.worker.qa as qa_module
    from localplaud.db.models import Chunk, PlaudFile, Transcript, TranscriptRevision
    from localplaud.db.session import session_scope
    from localplaud.providers.service import resolve_recording_profile

    vector = np.asarray([1.0, 0.0], dtype=np.float32)
    with session_scope() as session:
        recording = PlaudFile(id="r1", filename="Meeting")
        transcript = Transcript(
            file_id="r1",
            provider="fake",
            model="fake",
            source="local",
            text="Launch Friday",
            segments=[],
        )
        session.add_all([recording, transcript])
        session.flush()
        session.add(
            Chunk(
                file_id="r1",
                idx=0,
                text="Launch Friday",
                embedding=vector.tobytes(),
                dim=2,
                input_transcript_id=transcript.id,
                input_transcript_revision=0,
                input_transcript_source="local",
                resolved_profile_snapshot=resolve_recording_profile(session, "r1").to_dict(),
            )
        )
        transcript_id = transcript.id

    monkeypatch.setattr(qa_module, "build_embedder", lambda _cfg: _FakeEmbedder())
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0.0, {}))
    provider_calls = 0

    class MustNotRun:
        def complete(self, *_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            return "stale"

    monkeypatch.setattr(qa_module, "build_llm", lambda _cfg: MustNotRun())
    dispatches = 0

    def revise_before_dispatch():
        nonlocal dispatches
        dispatches += 1
        if dispatches == 2:
            with session_scope() as session:
                session.add(
                    TranscriptRevision(
                        file_id="r1",
                        base_transcript_id=transcript_id,
                        revision=1,
                        source="local",
                        text="Launch Monday",
                        segments=[],
                    )
                )

    with qa_module.provider_dispatch_guard(revise_before_dispatch):
        with pytest.raises(RuntimeError, match="evidence changed"):
            qa_module.answer("When?", file_id="r1")
    assert dispatches == 2
    assert provider_calls == 0


def test_forced_alignment_timing_change_before_llm_egress_blocks_provider(
    monkeypatch, tmp_path
):
    _fresh_db(monkeypatch, tmp_path)
    import localplaud.worker.qa as qa_module
    from localplaud.db.models import Chunk, PlaudFile, Transcript
    from localplaud.db.session import session_scope
    from localplaud.providers.service import resolve_recording_profile

    vector = np.asarray([1.0, 0.0], dtype=np.float32)
    with session_scope() as session:
        recording = PlaudFile(id="r1", filename="Meeting")
        transcript = Transcript(
            file_id="r1",
            provider="fake",
            model="fake",
            source="local",
            text="Launch Friday",
            segments=[{"text": "Launch Friday", "start": 1.0, "end": 2.0}],
        )
        session.add_all([recording, transcript])
        session.flush()
        session.add(
            Chunk(
                file_id="r1",
                idx=0,
                text="Launch Friday",
                start=1.0,
                end=2.0,
                embedding=vector.tobytes(),
                dim=2,
                input_transcript_id=transcript.id,
                input_transcript_revision=0,
                input_transcript_source="local",
                resolved_profile_snapshot=resolve_recording_profile(session, "r1").to_dict(),
            )
        )
        transcript_id = transcript.id

    monkeypatch.setattr(qa_module, "build_embedder", lambda _cfg: _FakeEmbedder())
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0.0, {}))
    provider_calls = 0

    class MustNotRun:
        def complete(self, *_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            return "stale"

    monkeypatch.setattr(qa_module, "build_llm", lambda _cfg: MustNotRun())
    dispatches = 0

    def align_before_dispatch():
        nonlocal dispatches
        dispatches += 1
        if dispatches == 2:
            with session_scope() as session:
                transcript = session.get(Transcript, transcript_id)
                transcript.segments = [
                    {"text": "Launch Friday", "start": 5.0, "end": 6.0}
                ]

    with qa_module.provider_dispatch_guard(align_before_dispatch):
        with pytest.raises(RuntimeError, match="evidence changed"):
            qa_module.answer("When?", file_id="r1")
    assert dispatches == 2
    assert provider_calls == 0


def test_transcript_evidence_is_revalidated_after_provider_before_final_save(
    monkeypatch, tmp_path
):
    _fresh_db(monkeypatch, tmp_path)
    _seed_two_files()
    import localplaud.worker.qa as qa_module
    from localplaud.db.models import Chunk
    from localplaud.db.session import session_scope

    monkeypatch.setattr(qa_module, "build_embedder", lambda _cfg: _FakeEmbedder())
    monkeypatch.setattr(qa_module, "build_llm", lambda _cfg: _FakeLlm())
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0.0, {}))

    with qa_module.provider_dispatch_guard(lambda: None) as dispatch_state:
        result = qa_module.answer("When?", file_id="r1")
    assert result["sources"]
    assert dispatch_state["evidence_fingerprints"]

    with session_scope() as session:
        chunk = session.query(Chunk).filter_by(file_id="r1", idx=0).one()
        chunk.text = "Changed after the provider returned"
    with session_scope() as session:
        with pytest.raises(RuntimeError, match="evidence changed"):
            qa_module.validate_evidence_fingerprints(
                session, dispatch_state["evidence_fingerprints"]
            )


def test_library_tag_membership_change_before_llm_egress_blocks_provider(
    monkeypatch, tmp_path
):
    _fresh_db(monkeypatch, tmp_path)
    _seed_two_files()
    import localplaud.worker.qa as qa_module
    from localplaud.db.models import PlaudFile, Tag
    from localplaud.db.session import session_scope

    with session_scope() as session:
        tag = Tag(name="Decision")
        session.add(tag)
        session.flush()
        recording = session.get(PlaudFile, "r1")
        recording.tags.append(tag)
        tag_id = tag.id

    monkeypatch.setattr(qa_module, "build_embedder", lambda _cfg: _FakeEmbedder())
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0.0, {}))
    provider_calls = 0

    class MustNotRun:
        def complete(self, *_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            return "out of scope"

    monkeypatch.setattr(qa_module, "build_llm", lambda _cfg: MustNotRun())
    dispatches = 0

    def remove_membership_before_dispatch():
        nonlocal dispatches
        dispatches += 1
        if dispatches == 2:
            with session_scope() as session:
                row = session.get(PlaudFile, "r1")
                row.tags.remove(session.get(Tag, tag_id))

    with qa_module.provider_dispatch_guard(remove_membership_before_dispatch):
        with pytest.raises(RuntimeError, match="evidence changed"):
            qa_module.answer("When?", retrieval_scope={"tag_id": tag_id})
    assert dispatches == 2
    assert provider_calls == 0


def test_public_ask_evidence_guard_covers_recording_and_library_leases(
    monkeypatch, tmp_path
):
    from datetime import UTC, datetime, timedelta

    _fresh_db(monkeypatch, tmp_path)
    from localplaud.db.models import AskThread, PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.providers.usage import lock_cost_budget
    from localplaud.worker.knowledge_index import (
        KnowledgeIndexBusyError,
        reject_active_ask_evidence_mutation,
    )

    with session_scope() as session:
        session.add(PlaudFile(id="r1", filename="Meeting"))
        session.add_all(
            [
                AskThread(
                    id="recording-ask",
                    file_id="r1",
                    title="Recording",
                    request_token="recording-token",
                    request_lease_until=datetime.now(UTC) + timedelta(minutes=5),
                ),
                AskThread(
                    id="expired-library-ask",
                    file_id=None,
                    title="Library",
                    request_token="expired-token",
                    request_lease_until=datetime.now(UTC) - timedelta(seconds=1),
                ),
            ]
        )

    with session_scope() as session:
        lock_cost_budget(session, "r1")
        with pytest.raises(KnowledgeIndexBusyError, match="used by Ask"):
            reject_active_ask_evidence_mutation(session, "r1")

    with session_scope() as session:
        thread = session.get(AskThread, "recording-ask")
        thread.request_lease_until = datetime.now(UTC) - timedelta(seconds=1)
    with session_scope() as session:
        lock_cost_budget(session, "r1")
        reject_active_ask_evidence_mutation(session, "r1")


def test_pipeline_transcript_write_rejects_active_ask_lease(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    _fresh_db(monkeypatch, tmp_path)
    from sqlalchemy import select

    from localplaud.asr.base import Segment
    from localplaud.asr.base import Transcript as TranscriptPayload
    from localplaud.db.models import AskThread, PlaudFile, Transcript
    from localplaud.db.session import session_scope
    from localplaud.worker.claims import processing_claim
    from localplaud.worker.knowledge_index import KnowledgeIndexBusyError
    from localplaud.worker.pipeline import _persist_aligned_transcript

    with session_scope() as session:
        recording = PlaudFile(
            id="r1",
            filename="Meeting",
            processing_token="pipeline-token",
            processing_lease_until=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(recording)
        session.add(
            Transcript(
                file_id="r1",
                provider="fake",
                source="local",
                text="Evidence",
                segments=[{"text": "Evidence", "start": 1.0, "end": 2.0}],
            )
        )
        session.add(
            AskThread(
                id="active-ask",
                file_id="r1",
                title="Active",
                request_token="ask-token",
                request_lease_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        )

    aligned = TranscriptPayload(
        segments=[Segment(text="Evidence", start=5.0, end=6.0)],
        provider="fake-align",
    )
    with processing_claim("r1", "pipeline-token"):
        with pytest.raises(KnowledgeIndexBusyError, match="used by Ask"):
            _persist_aligned_transcript("r1", aligned)

    with session_scope() as session:
        raw = session.scalar(select(Transcript).where(Transcript.file_id == "r1"))
        assert raw.segments[0]["start"] == 1.0


@pytest.mark.parametrize("mutation", ["edit", "delete"])
def test_note_mutation_between_retrieval_and_llm_egress_blocks_provider(
    monkeypatch, tmp_path, mutation
):
    _fresh_db(monkeypatch, tmp_path)
    import localplaud.worker.qa as qa_module
    from localplaud.db.models import KnowledgeChunk, PlaudFile, UserNote
    from localplaud.db.session import session_scope
    from localplaud.providers.service import resolve_recording_profile
    from localplaud.worker.knowledge_index import (
        delete_user_note_document,
        sync_user_note_document,
    )

    vector = np.asarray([1.0, 0.0], dtype=np.float32)
    with session_scope() as session:
        session.add(PlaudFile(id="r1", filename="Meeting"))
        session.flush()
        note = UserNote(file_id="r1", title="Plan", content_md="Launch Friday")
        session.add(note)
        session.flush()
        document = sync_user_note_document(session, note)
        snapshot = resolve_recording_profile(session, "r1").to_dict()
        document.status = "completed"
        document.profile_snapshot = snapshot
        session.add(
            KnowledgeChunk(
                document_id=document.id,
                idx=0,
                text="Launch Friday",
                embedding=vector.tobytes(),
                dim=2,
            )
        )
        note_id = note.id

    monkeypatch.setattr(qa_module, "build_embedder", lambda _cfg: _FakeEmbedder())
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0.0, {}))
    provider_calls = 0

    class MustNotRun:
        def complete(self, *_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            return "stale"

    monkeypatch.setattr(qa_module, "build_llm", lambda _cfg: MustNotRun())
    dispatches = 0

    def before_dispatch():
        nonlocal dispatches
        dispatches += 1
        if dispatches != 2:
            return
        with session_scope() as session:
            note = session.get(UserNote, note_id)
            if mutation == "delete":
                delete_user_note_document(session, note)
                session.delete(note)
            else:
                note.content_md = "Launch Monday"
                note.version += 1
                sync_user_note_document(session, note)

    with qa_module.provider_dispatch_guard(before_dispatch):
        with pytest.raises(RuntimeError, match="evidence changed"):
            qa_module.answer("When?", file_id="r1")
    assert dispatches == 2
    assert provider_calls == 0


def test_generated_note_regeneration_before_llm_egress_blocks_provider(
    monkeypatch, tmp_path
):
    _fresh_db(monkeypatch, tmp_path)
    import localplaud.worker.qa as qa_module
    from localplaud.db.models import KnowledgeChunk, PlaudFile, Summary, Transcript
    from localplaud.db.session import session_scope
    from localplaud.providers.service import resolve_recording_profile
    from localplaud.worker.knowledge_index import sync_summary_document

    with session_scope() as session:
        session.add(PlaudFile(id="r1", filename="Meeting"))
        transcript = Transcript(
            file_id="r1",
            provider="fake",
            model="fake",
            source="local",
            text="Launch discussion",
            segments=[],
        )
        session.add(transcript)
        session.flush()
        summary = Summary(
            file_id="r1",
            template="meeting",
            title="Plan",
            content_md="Launch Friday",
            source="local",
            input_transcript_id=transcript.id,
            input_transcript_revision=0,
            input_transcript_source="local",
        )
        session.add(summary)
        session.flush()
        document = sync_summary_document(session, summary)
        document.status = "completed"
        document.profile_snapshot = resolve_recording_profile(session, "r1").to_dict()
        session.add(
            KnowledgeChunk(
                document_id=document.id,
                idx=0,
                text="Launch Friday",
                embedding=np.asarray([1.0, 0.0], dtype=np.float32).tobytes(),
                dim=2,
            )
        )
        summary_id = summary.id

    monkeypatch.setattr(qa_module, "build_embedder", lambda _cfg: _FakeEmbedder())
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0.0, {}))
    provider_calls = 0

    class MustNotRun:
        def complete(self, *_args, **_kwargs):
            nonlocal provider_calls
            provider_calls += 1
            return "stale"

    monkeypatch.setattr(qa_module, "build_llm", lambda _cfg: MustNotRun())
    dispatches = 0

    def regenerate_before_dispatch():
        nonlocal dispatches
        dispatches += 1
        if dispatches == 2:
            with session_scope() as session:
                summary = session.get(Summary, summary_id)
                summary.content_md = "Launch Monday"
                summary.restored_from_revision = 2
                sync_summary_document(session, summary)

    with qa_module.provider_dispatch_guard(regenerate_before_dispatch):
        with pytest.raises(RuntimeError, match="evidence changed"):
            qa_module.answer("When?", file_id="r1")
    assert dispatches == 2
    assert provider_calls == 0


def test_internal_note_evidence_fingerprint_is_not_returned_as_a_citation(
    monkeypatch, tmp_path
):
    _fresh_db(monkeypatch, tmp_path)
    import localplaud.worker.qa as qa_module
    from localplaud.db.models import KnowledgeChunk, PlaudFile, UserNote
    from localplaud.db.session import session_scope
    from localplaud.providers.service import resolve_recording_profile
    from localplaud.worker.knowledge_index import sync_user_note_document

    with session_scope() as session:
        session.add(PlaudFile(id="r1", filename="Meeting"))
        session.flush()
        note = UserNote(file_id="r1", title="Plan", content_md="Launch Friday")
        session.add(note)
        session.flush()
        document = sync_user_note_document(session, note)
        document.status = "completed"
        document.profile_snapshot = resolve_recording_profile(session, "r1").to_dict()
        session.add(
            KnowledgeChunk(
                document_id=document.id,
                idx=0,
                text="Launch Friday",
                embedding=np.asarray([1.0, 0.0], dtype=np.float32).tobytes(),
                dim=2,
            )
        )

    monkeypatch.setattr(qa_module, "build_embedder", lambda _cfg: _FakeEmbedder())
    monkeypatch.setattr(qa_module, "build_llm", lambda _cfg: _FakeLlm())
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0.0, {}))
    result = qa_module.answer("When?", file_id="r1")
    assert result["sources"]
    assert all("_evidence_fingerprint" not in source for source in result["sources"])


def test_retrieve_applies_combined_library_scope(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _seed_two_files()
    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda cfg: _FakeEmbedder())
    from localplaud.db.models import Folder, PlaudFile, Speaker, Tag
    from localplaud.db.session import session_scope
    from localplaud.worker.qa import normalize_library_scope, retrieve

    with session_scope() as session:
        folder = Folder(name="Research")
        tag = Tag(name="Priority")
        session.add_all([folder, tag])
        session.flush()
        first = session.get(PlaudFile, "r1")
        first.folder_id = folder.id
        first.tags.append(tag)
        first.origin = "plaud"
        first.start_time_ms = 1_767_225_600_000  # 2026-01-01 UTC
        second = session.get(PlaudFile, "r2")
        second.origin = "local"
        second.start_time_ms = 1_735_689_600_000  # 2025-01-01 UTC
        session.add_all(
            [
                Speaker(file_id="r1", key="SPEAKER_00", display_name="Sky"),
                Speaker(file_id="r2", key="SPEAKER_00", display_name="Alex"),
            ]
        )
        folder_id, tag_id = folder.id, tag.id

    scope = {
        "folder_id": folder_id,
        "tag_id": tag_id,
        "origin": "plaud",
        "speaker_name": "Sky",
        "date_from": "2026-01-01",
        "date_to": "2026-12-31",
    }
    hits = retrieve("q", top_k=6, retrieval_scope=scope)
    assert hits and {item["file_id"] for item in hits} == {"r1"}
    assert normalize_library_scope(scope) == scope | {
        "scope_version": 1,
        "date_timezone": "UTC",
        "date_from_ms": 1_767_225_600_000,
        "date_to_ms_exclusive": 1_798_761_600_000,
    }
    with pytest.raises(ValueError, match="origin"):
        normalize_library_scope({"origin": "external"})
    with pytest.raises(ValueError, match="date_from"):
        normalize_library_scope({"date_from": "2026-12-31", "date_to": "2026-01-01"})
    with pytest.raises(ValueError, match="supported range"):
        normalize_library_scope({"date_to": "9999-12-31"})
    frozen = {
        "scope_version": 2,
        "date_timezone": "Asia/Taipei",
        "date_from": "2026-07-01",
        "date_from_ms": 1_782_835_200_000,
    }
    assert normalize_library_scope(frozen) == frozen
    with pytest.raises(ValueError, match="does not match"):
        normalize_library_scope(frozen | {"date_from_ms": 1_782_835_200_001})


def test_retrieve_excludes_trash_before_ranking(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _seed_two_files()
    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda cfg: _FakeEmbedder())
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope
    from localplaud.worker.qa import retrieve

    with session_scope() as session:
        session.get(PlaudFile, "r1").is_trash = True

    hits = retrieve("q", top_k=6)
    assert [item["file_id"] for item in hits] == ["r2"]
    single_file_hits = retrieve("q", file_id="r1", top_k=6)
    assert single_file_hits
    assert {item["file_id"] for item in single_file_hits} == {"r1"}


def test_answer_source_shape_and_scope(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    _seed_two_files()
    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda cfg: _FakeEmbedder())
    monkeypatch.setattr("localplaud.worker.qa.build_llm", lambda cfg: _FakeLlm())
    from localplaud.worker.qa import answer

    res = answer("q", file_id="r1")
    assert res["answer"] == "Grounded answer."
    assert res["sources"]
    top = res["sources"][0]
    for key in ("start", "end", "file_id", "filename", "speaker", "score", "text"):
        assert key in top
    assert all(s["file_id"] == "r1" for s in res["sources"])


def test_remote_only_embedding_profile_can_query_existing_index(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    import localplaud.worker.pipeline as pipeline
    import localplaud.worker.qa as qa_module

    snapshot = {
        "stages": {
            "embed": {
                "connection": "worker:gpu",
                "model": "remote-embed",
                "execution_target": "remote_worker",
            },
            "ask": {
                "connection": "llm:fake",
                "model": "fake",
                "execution_target": "local",
            },
        },
        "policy": {},
    }
    _seed_two_files(snapshot)
    monkeypatch.setattr(qa_module, "_resolved_snapshot", lambda _file_id: snapshot)
    monkeypatch.setattr(qa_module, "candidate_snapshots", lambda *_args: [snapshot])
    monkeypatch.setattr(qa_module, "_settings_for_stage", lambda settings, *_args: settings)
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0, {}))
    monkeypatch.setattr(qa_module, "build_llm", lambda _cfg: _FakeLlm())

    def fake_remote(_file_id, _snapshot, stage, inputs):
        query = inputs[0].value["segments"][0]["text"]
        assert stage == "embed"
        return {
            "chunks": [{"text": query}],
            "vectors_base64": [
                base64.b64encode(np.asarray([1.0, 0.0], dtype=np.float32).tobytes()).decode()
            ],
            "model": "remote-embed",
            "dim": 2,
        }

    monkeypatch.setattr(pipeline, "_run_remote_stage", fake_remote)
    result = qa_module.answer("q", file_id="r1")
    assert result["answer"] == "Grounded answer."
    assert result["sources"] and result["sources"][0]["file_id"] == "r1"

    monkeypatch.setattr(
        pipeline,
        "_run_remote_stage",
        lambda *_args, **_kwargs: {
            "chunks": [{"text": "q"}],
            "vectors_base64": [
                base64.b64encode(np.asarray([1.0, 0.0], dtype=np.float32).tobytes()).decode()
            ],
            "model": "wrong-space",
            "dim": 2,
        },
    )
    with pytest.raises(ValueError, match="different model"):
        qa_module._remote_query_vector("q", snapshot, "r1")

    monkeypatch.setattr(
        pipeline,
        "_run_remote_stage",
        lambda *_args, **_kwargs: {
            "chunks": [{"text": "q"}],
            "vectors_base64": [
                base64.b64encode(np.asarray([1.0, 0.0], dtype=np.float32).tobytes()).decode()
            ],
            "dim": 2,
        },
    )
    with pytest.raises(ValueError, match="returned no model"):
        qa_module._remote_query_vector("q", snapshot, "r1")


def test_answer_no_chunks_degrades(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="empty", filename="No Index"))
    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda cfg: _FakeEmbedder())
    from localplaud.worker.qa import answer

    res = answer("q", file_id="empty")
    assert res["sources"] == []
    assert "isn't indexed" in res["answer"]


def test_query_embedding_retries_matching_fallback_after_empty_primary(monkeypatch):
    import localplaud.worker.qa as qa_module
    from localplaud.config import Settings

    snapshot = {
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
    calls: list[str] = []

    def fake_retrieve(*_args, **kwargs):
        model = kwargs["embedding_snapshot"]["stages"]["embed"]["model"]
        calls.append(model)
        return [] if model == "primary" else [{"file_id": "r1", "score": 1.0}]

    monkeypatch.setattr(qa_module, "retrieve", fake_retrieve)
    monkeypatch.setattr(qa_module, "_settings_for_stage", lambda settings, *_args: settings)
    monkeypatch.setattr(
        qa_module,
        "_candidate_cost",
        lambda *_args: (0.01, {"per_request_usd": 0.01}),
    )
    hits, selected, usage, cost = qa_module._retrieve_with_profile(
        "question", 6, Settings(), "r1", snapshot, 0, None
    )
    assert calls == ["primary", "fallback"]
    assert hits and selected["stages"]["embed"]["model"] == "fallback"
    assert usage["requests"] == 2
    assert cost == 0.02


def test_query_embedding_searches_all_current_fallback_spaces(monkeypatch):
    import localplaud.worker.qa as qa_module
    from localplaud.config import Settings

    snapshot = {
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
    calls: list[str] = []

    def fake_retrieve(*_args, **kwargs):
        model = kwargs["embedding_snapshot"]["stages"]["embed"]["model"]
        calls.append(model)
        return [
            {
                "file_id": model,
                "target": "transcript",
                "text": f"{model} evidence",
                "start": 0.0,
                "end": 1.0,
                "score": 0.9 if model == "primary" else 0.8,
            }
        ]

    monkeypatch.setattr(qa_module, "retrieve", fake_retrieve)
    monkeypatch.setattr(qa_module, "_settings_for_stage", lambda settings, *_args: settings)
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0.0, {}))
    hits, selected, usage, cost = qa_module._retrieve_with_profile(
        "question", 6, Settings(), None, snapshot, 0, None
    )
    assert calls == ["primary", "fallback"]
    assert [hit["file_id"] for hit in hits] == ["primary", "fallback"]
    assert selected["stages"]["embed"]["model"] == "primary"
    assert [item["stages"]["embed"]["model"] for item in selected["queried_profiles"]] == [
        "primary",
        "fallback",
    ]
    assert [hit["embedding_identity"]["model"] for hit in hits] == [
        "primary",
        "fallback",
    ]
    assert usage["requests"] == 2
    assert cost == 0


def test_query_embedding_fuses_ranks_instead_of_cross_model_cosine(monkeypatch):
    import localplaud.worker.qa as qa_module
    from localplaud.config import Settings

    snapshot = {
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

    def fake_retrieve(*_args, **kwargs):
        model = kwargs["embedding_snapshot"]["stages"]["embed"]["model"]
        raw_score = 0.1 if model == "primary" else 0.99
        return [{"file_id": model, "text": f"{model} evidence", "score": raw_score}]

    monkeypatch.setattr(qa_module, "retrieve", fake_retrieve)
    monkeypatch.setattr(qa_module, "_settings_for_stage", lambda settings, *_args: settings)
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0.0, {}))

    hits, _selected, _usage, _cost = qa_module._retrieve_with_profile(
        "question", 6, Settings(), None, snapshot, 0, None
    )

    assert [hit["file_id"] for hit in hits] == ["primary", "fallback"]
    assert hits[0]["score"] == hits[1]["score"]
    assert hits[0]["embedding_score"] == 0.1
    assert hits[1]["embedding_score"] == 0.99
    assert all(hit["embedding_rank"] == 1 for hit in hits)


def test_query_embedding_keeps_primary_hits_when_fallback_space_fails(monkeypatch):
    import localplaud.worker.qa as qa_module
    from localplaud.config import Settings
    from localplaud.embeddings.base import EmbeddingUnavailable

    snapshot = {
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
    calls: list[str] = []

    def fake_retrieve(*_args, **kwargs):
        model = kwargs["embedding_snapshot"]["stages"]["embed"]["model"]
        calls.append(model)
        if model == "fallback":
            raise EmbeddingUnavailable("fallback space unavailable")
        return [{"file_id": "primary", "text": "valid evidence", "score": 0.9}]

    monkeypatch.setattr(qa_module, "retrieve", fake_retrieve)
    monkeypatch.setattr(qa_module, "_settings_for_stage", lambda settings, *_args: settings)
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0.0, {}))

    hits, selected, usage, cost = qa_module._retrieve_with_profile(
        "question", 6, Settings(), None, snapshot, 0, None
    )

    assert calls == ["primary", "fallback"]
    assert [hit["file_id"] for hit in hits] == ["primary"]
    assert selected["stages"]["embed"]["model"] == "primary"
    assert selected["fallback_failures"] == [
        {
            "index": 1,
            "connection": "embeddings:fallback",
            "model": "fallback",
            "error": "fallback space unavailable",
            "retryable": True,
        }
    ]
    assert usage["requests"] == 1
    assert cost == 0


def test_query_embedding_keeps_hits_when_fallback_configuration_fails(monkeypatch):
    import localplaud.worker.qa as qa_module
    from localplaud.config import Settings
    from localplaud.embeddings.base import EmbeddingUnavailable

    snapshot = {
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

    def fake_settings(current, candidate, _stage):
        if candidate["stages"]["embed"]["model"] == "fallback":
            raise EmbeddingUnavailable("fallback configuration unavailable")
        return current

    monkeypatch.setattr(qa_module, "_settings_for_stage", fake_settings)
    monkeypatch.setattr(
        qa_module,
        "retrieve",
        lambda *_args, **_kwargs: [{"file_id": "primary", "text": "valid evidence", "score": 0.9}],
    )
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0.0, {}))

    hits, selected, usage, _cost = qa_module._retrieve_with_profile(
        "question", 6, Settings(), None, snapshot, 0, None
    )

    assert [hit["file_id"] for hit in hits] == ["primary"]
    assert selected["fallback_failures"][0]["error"] == ("fallback configuration unavailable")
    assert usage["requests"] == 1


def test_answer_persists_actual_embedding_retrieval_profile(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    import localplaud.worker.qa as qa_module
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    snapshot = {
        "stages": {
            "embed": {"connection": "embed:primary", "model": "primary"},
            "ask": {"connection": "llm:primary", "model": "answer"},
        },
        "policy": {},
    }
    retrieval_profile = {
        "stages": {
            "embed": {"connection": "embed:fallback", "model": "fallback"},
            "ask": {"connection": "llm:primary", "model": "answer"},
        },
        "policy": {},
        "fallback_failures": [
            {
                "index": 0,
                "connection": "embed:primary",
                "model": "primary",
                "error": "primary unavailable",
                "retryable": True,
            }
        ],
    }
    with session_scope() as session:
        session.add(PlaudFile(id="r1", filename="Meeting"))
    monkeypatch.setattr(qa_module, "_resolved_snapshot", lambda _file_id: snapshot)
    monkeypatch.setattr(
        qa_module,
        "_retrieve_with_profile",
        lambda *_args, **_kwargs: (
            [
                {
                    "file_id": "r1",
                    "filename": "Meeting",
                    "text": "Grounded evidence",
                    "score": 1.0,
                    "start": 0.0,
                    "end": 1.0,
                    "speaker": None,
                }
            ],
            retrieval_profile,
            {"requests": 2},
            0.0,
        ),
    )
    monkeypatch.setattr(qa_module, "_settings_for_stage", lambda current, *_args: current)
    monkeypatch.setattr(qa_module, "_candidate_cost", lambda *_args: (0.0, {}))

    class Llm:
        def complete(self, *_args, **_kwargs):
            return "Grounded answer"

    monkeypatch.setattr(qa_module, "build_llm", lambda *_args: Llm())
    result = qa_module.answer("What happened?", file_id="r1")

    profile = result["provenance"]["profile"]
    assert profile["stages"]["embed"]["model"] == "fallback"
    assert profile["retrieval_profile"] == retrieval_profile
    assert profile["retrieval_profile"]["fallback_failures"][0]["model"] == "primary"
    assert profile["fallback_failures"] == []


def test_legacy_transcript_vectors_fail_closed_and_requeue(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    from sqlalchemy import select

    from localplaud.db.models import Chunk, PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker.knowledge_index import sync_transcript_index_profiles
    from localplaud.worker.qa import answer

    with session_scope() as session:
        session.add(PlaudFile(id="legacy", filename="Legacy"))
        session.add(
            Chunk(
                file_id="legacy",
                idx=0,
                text="unproven legacy vector",
                embedding=np.asarray([1.0, 0.0], dtype=np.float32).tobytes(),
                dim=2,
                resolved_profile_snapshot=None,
            )
        )
    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda _cfg: _FakeEmbedder())
    result = answer("question", file_id="legacy")
    assert result["sources"] == []

    with session_scope() as session:
        assert sync_transcript_index_profiles(session, file_ids=["legacy"]) == ["legacy"]
    with session_scope() as session:
        assert session.query(Chunk).filter_by(file_id="legacy").count() == 0
        run = session.scalar(
            select(StageRun).where(StageRun.file_id == "legacy", StageRun.stage == StageName.index)
        )
        assert run.status == StageStatus.pending
        assert run.detail["reindex_only"] is True


def test_ask_fallback_persists_all_reserved_candidate_cost(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    import localplaud.worker.qa as qa_module
    from localplaud.db.models import PlaudFile, ProviderCostReservation
    from localplaud.db.session import session_scope
    from localplaud.llm.base import LLMUnavailable

    snapshot = {
        "stages": {
            "embed": {
                "connection": "embeddings:local",
                "model": "local",
                "execution_target": "local",
            },
            "ask": {
                "connection": "llm:primary",
                "model": "primary",
                "execution_target": "cloud",
            },
        },
        "policy": {
            "fallback_policy": {
                "stages": {
                    "ask": [
                        {
                            "connection": "llm:fallback",
                            "model": "fallback",
                            "execution_target": "cloud",
                        }
                    ]
                }
            }
        },
    }
    with session_scope() as session:
        session.add(PlaudFile(id="r1", filename="Meeting"))
    monkeypatch.setattr(qa_module, "_resolved_snapshot", lambda _file_id: snapshot)
    monkeypatch.setattr(
        qa_module,
        "_retrieve_with_profile",
        lambda *_args, **_kwargs: (
            [
                {
                    "file_id": "r1",
                    "filename": "Meeting",
                    "text": "Evidence",
                    "score": 1.0,
                    "start": 0.0,
                    "end": 1.0,
                    "speaker": None,
                }
            ],
            snapshot,
            {},
            0.0,
        ),
    )
    monkeypatch.setattr(qa_module, "_settings_for_stage", lambda current, *_args: current)

    def fake_cost(candidate, stage, _usage, _spent, reservation_id, file_id):
        amount = 0.06 if candidate["stages"][stage]["model"] == "primary" else 0.04
        with session_scope() as session:
            row = session.get(ProviderCostReservation, reservation_id)
            if row is None:
                row = ProviderCostReservation(
                    id=reservation_id,
                    scope_key=f"file:{file_id}",
                    file_id=file_id,
                    operation=stage,
                    status="active",
                    estimated_cost_usd=0,
                )
                session.add(row)
            row.estimated_cost_usd = float(row.estimated_cost_usd or 0) + amount
        return amount, {"per_request_usd": amount}

    class FailingLlm:
        def complete(self, *_args, **_kwargs):
            raise LLMUnavailable("primary unavailable")

    class WorkingLlm:
        def complete(self, *_args, **_kwargs):
            return "Grounded answer"

    llms = iter([FailingLlm(), WorkingLlm()])
    monkeypatch.setattr(qa_module, "_candidate_cost", fake_cost)
    monkeypatch.setattr(qa_module, "build_llm", lambda _settings: next(llms))
    result = qa_module.answer("Question", file_id="r1")
    assert result["answer"] == "Grounded answer"
    assert result["estimated_cost_usd"] == 0.10


def test_failed_ask_closes_uncertain_cost_reservation(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    from sqlalchemy import select

    import localplaud.worker.qa as qa_module
    from localplaud.db.models import PlaudFile, ProviderCostReservation
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(PlaudFile(id="r1", filename="Meeting"))

    def fail_after_reserving(*_args, reservation_id=None, file_id=None, **_kwargs):
        reservation_id = reservation_id or _args[7]
        file_id = file_id or _args[3]
        with session_scope() as session:
            session.add(
                ProviderCostReservation(
                    id=reservation_id,
                    scope_key=f"file:{file_id}",
                    file_id=file_id,
                    operation="embed",
                    status="active",
                    estimated_cost_usd=0.03,
                )
            )
        raise RuntimeError("provider outcome is uncertain")

    monkeypatch.setattr(qa_module, "_resolved_snapshot", lambda _file_id: {"stages": {}})
    monkeypatch.setattr(qa_module, "_retrieve_with_profile", fail_after_reserving)
    with pytest.raises(RuntimeError, match="uncertain"):
        qa_module.answer("Question", file_id="r1")

    with session_scope() as session:
        reservation = session.scalar(select(ProviderCostReservation))
        assert reservation.status == "failed"
        assert reservation.completed_at is not None
        assert reservation.estimated_cost_usd == pytest.approx(0.03)


# --------------------------------------------------------------------------- #
# web fragment
# --------------------------------------------------------------------------- #


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    _fresh_db(monkeypatch, tmp_path, name="ui.db")
    from localplaud.api.app import app

    return TestClient(app)


def _seed_file():
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as s:
        s.add(PlaudFile(id="r1", filename="Weekly Sync", status=FileStatus.done))


def test_file_ask_renders_playable_citations(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed_file()

    def fake_answer(
        q,
        top_k=6,
        settings=None,
        file_id=None,
        history=None,
        spent_cost_usd=0,
        instruction=None,
    ):
        assert file_id == "r1"
        assert history == []
        return {
            "answer": "We shipped the beta.",
            "sources": [
                {
                    "score": 0.9,
                    "text": "we agreed to ship the beta",
                    "start": 42.0,
                    "end": 45.0,
                    "speaker": "SPEAKER_00",
                    "file_id": "r1",
                    "filename": "Weekly Sync",
                }
            ],
        }

    monkeypatch.setattr("localplaud.worker.qa.answer", fake_answer)
    r = c.post("/file/r1/ask", data={"q": "what was decided?"})
    assert r.status_code == 200
    assert "We shipped the beta." in r.text
    assert 'data-seek="42.0"' in r.text
    assert "0:42" in r.text  # mm:ss stamp


def test_file_ask_note_citation_links_to_artifact_without_fake_seek(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed_file()

    monkeypatch.setattr(
        "localplaud.worker.qa.answer",
        lambda *_args, **_kwargs: {
            "answer": "The saved plan says Friday.",
            "sources": [
                {
                    "target": "saved_note",
                    "artifact_id": 7,
                    "artifact_title": "Launch plan",
                    "artifact_version": 2,
                    "url": "/notes/7/versions/2",
                    "label": "Saved note · Launch plan",
                    "score": 0.9,
                    "text": "Launch on Friday",
                    "start": None,
                    "end": None,
                    "speaker": None,
                    "file_id": "r1",
                    "filename": "Weekly Sync",
                }
            ],
        },
    )
    response = c.post("/file/r1/ask", data={"q": "When do we launch?"})
    assert response.status_code == 200
    assert 'href="/notes/7/versions/2"' in response.text
    assert "Launch plan" in response.text
    assert "data-seek" not in response.text
    message_id = int(re.search(r"saveAskNote\((\d+)", response.text).group(1))
    saved = c.post(f"/api/ask/messages/{message_id}/save-note", json={})
    citation = saved.json()["citations"][0]
    assert citation["target"] == "saved_note"
    assert citation["artifact_id"] == 7
    assert citation["artifact_version"] == 2
    assert citation["url"] == "/notes/7/versions/2"


def test_file_ask_unknown_file_404(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed_file()
    assert c.post("/file/missing/ask", data={"q": "hi"}).status_code == 404


def test_file_ask_no_chunks_degrades(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed_file()
    # Real qa path: fake embedder, no chunks seeded -> friendly degraded message.
    monkeypatch.setattr("localplaud.worker.qa.build_embedder", lambda cfg: _FakeEmbedder())
    r = c.post("/file/r1/ask", data={"q": "anything?"})
    assert r.status_code == 200
    assert "indexed yet" in r.text  # apostrophe is HTML-escaped in the fragment
    assert "data-seek" not in r.text


def test_file_ask_provider_unavailable_degrades(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed_file()

    def boom(*a, **k):
        raise RuntimeError("no embeddings")

    monkeypatch.setattr("localplaud.worker.qa.answer", boom)
    r = c.post("/file/r1/ask", data={"q": "anything?"})
    assert r.status_code == 200
    assert "unavailable" in r.text.lower()


def test_detail_page_has_ask_tab_and_deeplink(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed_file()
    r = c.get("/file/r1")
    assert r.status_code == 200
    # Ask tab + panel wired to the single-file endpoint.
    assert 'data-panel="ask"' in r.text
    assert 'hx-post="/file/r1/ask"' in r.text
    assert 'id="file-answer"' in r.text
    assert 'data-ask-request data-ask-status="file-ask-status"' in r.text
    assert 'hx-sync="#file-answer:drop"' in r.text
    assert r.text.count('hx-sync="#file-answer:drop"') >= 2
    assert (
        'id="file-ask-status" class="ask-request-status" role="status" aria-live="polite"' in r.text
    )
    assert 'id="file-answer" role="region" aria-label="Answer"' in r.text
    assert "forms.some(candidate=>candidate.dataset.askBusy==='true')" in r.text
    assert "control.disabled=true" in r.text
    assert "window.localplaudT('Getting answer…')" in r.text
    assert "window.localplaudT('Answer ready')" in r.text
    assert "const askRequests=new WeakMap()" in r.text
    assert "askRequests.set(xhr,{forms,controls,status,target,question,focusTarget})" in r.text
    assert "Check History before retrying to avoid a duplicate conversation." in r.text
    assert "requestAnimationFrame(()=>{(question?.isConnected?question" in r.text
    # Suggested, grounded, non-mutating chips.
    assert "What was decided?" in r.text
    # Delegated seek handler + ?t= deep-link support.
    assert "data-seek" in r.text
    assert "URLSearchParams" in r.text
