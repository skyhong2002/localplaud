"""Mind-map-only rebuild: an explicit user action regenerates just the mind
map from the current canonical transcript and the current (generated or
restored) source note output. Notes, transcript rows and revisions, speech
stages, and the search index are never touched; the displaced map is archived
to version history; the stale flag clears only on success; a failure leaves a
scoped, actionable, retryable state that the background scanner resumes at the
same narrow scope."""

from __future__ import annotations

from sqlalchemy import select

SEGMENTS = [
    {"text": "hello team", "start": 1.0, "end": 2.0, "speaker": "SPEAKER_00", "words": []},
    {"text": "let's start", "start": 2.0, "end": 3.0, "speaker": "SPEAKER_01", "words": []},
]

LINEAGE = {
    "input_transcript_id": 1,
    "input_transcript_revision": 0,
    "input_transcript_source": "local",
}


def _reset_db(monkeypatch, tmp_path, **env):
    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'mm.db'}")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)


def _client(monkeypatch, tmp_path, **env):
    from fastapi.testclient import TestClient

    _reset_db(monkeypatch, tmp_path, **env)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed(
    *,
    with_note: bool = True,
    with_map: bool = True,
    with_transcript: bool = True,
    map_stale: bool = True,
    stale_detail: dict | None = None,
    audio_path: str | None = None,
):
    """A recording that already completed its pipeline, whose mind map (if
    any) has been marked out of date — the state the rebuild starts from."""
    from localplaud.db.models import (
        Chunk,
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Transcript,
    )
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _persist_summary

    with session_scope() as s:
        s.add(
            PlaudFile(
                id="r1",
                filename="Weekly Sync",
                status=FileStatus.done,
                duration_ms=600000,
                audio_path=audio_path,
                wav_path=None,
            )
        )
        if with_transcript:
            s.add(
                Transcript(
                    file_id="r1",
                    provider="fake-asr",
                    model="fake-model",
                    language="en",
                    has_speakers=True,
                    source="local",
                    text="hello team\nlet's start",
                    segments=SEGMENTS,
                )
            )
        s.add(Chunk(file_id="r1", idx=0, text="hello team let's start", start=1.0, end=3.0))
        s.add(
            StageRun(
                file_id="r1",
                stage=StageName.summarize,
                status=StageStatus.completed,
                attempts=1,
                provider="fake-llm",
                model="m-1",
                detail={"sentinel": "summarize"},
            )
        )
        s.add(
            StageRun(
                file_id="r1",
                stage=StageName.index,
                status=StageStatus.completed,
                attempts=1,
                provider="fake-embed",
                model="e-1",
                detail={"sentinel": "index"},
            )
        )
        if with_map:
            s.add(
                StageRun(
                    file_id="r1",
                    stage=StageName.mind_map,
                    status=StageStatus.pending if map_stale else StageStatus.completed,
                    attempts=1,
                    provider="fake-llm",
                    model="m-1",
                    detail=(
                        stale_detail
                        if stale_detail is not None
                        else {"stale": True, "reason": "note version restored"}
                    )
                    if map_stale
                    else {},
                )
            )
    if with_note:
        _persist_summary(
            "r1",
            {
                "template": "default",
                "title": "Sync notes",
                "content_md": "# Sync notes\n\n- agenda\n- decisions",
                "provider": "fake-llm",
                "model": "m-1",
                "template_version": 1,
                "template_snapshot": {"name": "Default", "version": 1},
            },
            dict(LINEAGE),
        )
    if with_map:
        _persist_summary(
            "r1",
            {
                "template": "mind_map",
                "title": None,
                "content_md": "# Sync topics\n- agenda\n  - budget",
                "provider": "fake-llm",
                "model": "m-1",
                "template_snapshot": {
                    "source_template_key": "default",
                    "source_template_version": 1,
                },
            },
            dict(LINEAGE),
        )


def _install_llm_fakes(monkeypatch, counters):
    def fake_summary(transcript, settings):
        counters["sum"] += 1
        return {
            "title": "T",
            "content_md": "# T\n\nbody",
            "provider": "fake",
            "model": "m",
            "template": settings.pipeline.summary_template,
        }

    def fake_mindmap(transcript, settings, summary_md=None):
        counters["mm"] += 1
        counters["mm_summary_md"] = summary_md
        return {
            "template": "mind_map",
            "title": None,
            "content_md": "# Rebuilt\n- fresh point",
            "provider": "fake",
            "model": "m",
            "detail": {"outline_nodes": 2},
        }

    def fake_embed(chunks, settings):
        counters["emb"] += 1
        return [b"\x00\x00\x80?" for _ in chunks], "fake", 1

    monkeypatch.setattr("localplaud.worker.pipeline.summarize.summarize", fake_summary)
    monkeypatch.setattr("localplaud.worker.pipeline.mindmap.generate_mind_map", fake_mindmap)
    monkeypatch.setattr("localplaud.worker.pipeline.index.embed_chunks", fake_embed)
    return counters


def _snapshot_state(session):
    """Everything the rebuild must not touch, in comparable form."""
    from localplaud.db.models import (
        Chunk,
        StageName,
        StageRun,
        Summary,
        SummaryRevision,
        Transcript,
    )

    notes = [
        (s.id, s.template, s.content_md, s.created_at, s.restored_from_revision)
        for s in session.scalars(
            select(Summary).where(Summary.file_id == "r1", Summary.template != "mind_map")
        )
    ]
    revisions = [
        (r.id, r.template, r.revision, r.content_md, r.archive_reason)
        for r in session.scalars(
            select(SummaryRevision)
            .where(SummaryRevision.file_id == "r1", SummaryRevision.template != "mind_map")
            .order_by(SummaryRevision.id)
        )
    ]
    transcripts = [
        (t.id, t.source, t.text)
        for t in session.scalars(select(Transcript).where(Transcript.file_id == "r1"))
    ]
    chunks = [
        (c.id, c.text) for c in session.scalars(select(Chunk).where(Chunk.file_id == "r1"))
    ]
    other_runs = [
        (run.stage.value, run.status.value, run.attempts, run.detail, run.error)
        for run in session.scalars(select(StageRun).where(StageRun.file_id == "r1"))
        if run.stage != StageName.mind_map
    ]
    return {
        "notes": notes,
        "revisions": revisions,
        "transcripts": transcripts,
        "chunks": chunks,
        "other_runs": other_runs,
    }


# ---------------------------------------------------------------------------
# Worker path


def test_remote_mind_map_idempotency_includes_effective_note_options(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from types import SimpleNamespace

    from localplaud.db.models import ProviderConnection
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker import pipeline

    init_db()
    with session_scope() as s:
        s.add(
            ProviderConnection(
                key="worker:test",
                name="Test worker",
                provider_type="remote-worker",
                execution_target="remote_worker",
                data_egress=True,
                config={"base_url": "https://worker.invalid"},
            )
        )

    requests = []

    class FakeClient:
        def submit_and_wait(self, request, timeout):
            requests.append(request)
            return SimpleNamespace(artifacts={"result.json": b"{}"})

        def close(self):
            pass

    monkeypatch.setattr(
        pipeline.RemoteWorkerClient, "from_config", lambda _config: FakeClient()
    )
    snapshot = {
        "stages": {
            "mind_map": {
                "connection": "worker:test",
                "model": "map-model",
                "execution_target": "remote_worker",
                "options": {"language": "en"},
            }
        }
    }
    inputs = [pipeline._remote_json_input("transcript", {"segments": []})]
    for note in ("# Note A", "# Note B", "# Note A"):
        pipeline._run_remote_stage(
            "r1", snapshot, "mind_map", inputs, options={"summary_md": note}
        )

    keys = [request.idempotency_key for request in requests]
    assert keys[0] != keys[1]
    assert keys[0] == keys[2]


def test_rebuild_success_replaces_only_the_map_with_provenance(monkeypatch, tmp_path):
    """A successful rebuild archives the displaced map, records immutable
    source-note provenance, clears the stale flag, and leaves every other
    artifact and stage byte-identical. No audio is required."""
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageAttempt,
        StageName,
        StageStatus,
        Summary,
        SummaryRevision,
    )
    from localplaud.db.session import init_db, session_scope
    from localplaud.note_history import fingerprint_digest
    from localplaud.worker.pipeline import process_mind_map_only

    init_db()
    _seed(audio_path=None)
    counters = _install_llm_fakes(monkeypatch, {"sum": 0, "mm": 0, "emb": 0})
    with session_scope() as s:
        before = _snapshot_state(s)
        note = s.scalars(
            select(Summary).where(Summary.file_id == "r1", Summary.template == "default")
        ).one()
        note_fingerprint = fingerprint_digest(note)
        old_map_content = s.scalars(
            select(Summary).where(Summary.file_id == "r1", Summary.template == "mind_map")
        ).one().content_md

    process_mind_map_only("r1")

    assert counters["sum"] == 0 and counters["emb"] == 0 and counters["mm"] == 1
    # The generator received exactly the live source note content.
    assert counters["mm_summary_md"] == "# Sync notes\n\n- agenda\n- decisions"
    with session_scope() as s:
        assert _snapshot_state(s) == before  # notes/revisions/transcripts/chunks untouched
        recording = s.get(PlaudFile, "r1")
        assert recording.status == FileStatus.done
        assert recording.error is None
        assert recording.processing_token is None and recording.processing_lease_until is None
        assert recording.pipeline_retry_count == 0 and recording.pipeline_next_retry_at is None

        live_map = s.scalars(
            select(Summary).where(Summary.file_id == "r1", Summary.template == "mind_map")
        ).one()
        assert live_map.content_md == "# Rebuilt\n- fresh point"
        snapshot = live_map.template_snapshot
        assert snapshot["source_template_key"] == "default"
        assert snapshot["source_template_version"] == 1
        # Immutable provenance of the exact source note input.
        source_note = snapshot["source_note"]
        assert source_note["template"] == "default"
        assert source_note["template_version"] == 1
        assert source_note["llm_provider"] == "fake-llm"
        assert source_note["model"] == "m-1"
        assert source_note["created_at"]
        assert source_note["content_fingerprint"] == note_fingerprint
        assert "restored_from_revision" not in source_note

        # The displaced map is preserved in version history.
        map_revisions = list(
            s.scalars(
                select(SummaryRevision).where(
                    SummaryRevision.file_id == "r1", SummaryRevision.template == "mind_map"
                )
            )
        )
        assert [r.archive_reason for r in map_revisions] == ["regenerated"]
        assert map_revisions[0].content_md == old_map_content

        run = next(r for r in recording.stage_runs if r.stage == StageName.mind_map)
        assert run.status == StageStatus.completed
        assert run.attempts == 2  # history preserved, one new attempt
        assert run.error is None
        assert not (run.detail or {}).get("stale")
        assert "mind_map_only" not in (run.detail or {})
        attempts = list(
            s.scalars(select(StageAttempt).where(StageAttempt.file_id == "r1"))
        )
        assert [a.stage for a in attempts] == [StageName.mind_map]


def test_rebuild_records_restored_revision_in_provenance(monkeypatch, tmp_path):
    """When the source note is a restored version, the map records which one."""
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import Summary
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_mind_map_only

    init_db()
    _seed(audio_path=None)
    _install_llm_fakes(monkeypatch, {"sum": 0, "mm": 0, "emb": 0})
    with session_scope() as s:
        note = s.scalars(
            select(Summary).where(Summary.file_id == "r1", Summary.template == "default")
        ).one()
        note.restored_from_revision = 3

    process_mind_map_only("r1")

    with session_scope() as s:
        live_map = s.scalars(
            select(Summary).where(Summary.file_id == "r1", Summary.template == "mind_map")
        ).one()
        assert live_map.template_snapshot["source_note"]["restored_from_revision"] == 3


def test_rebuild_compare_and_set_rejects_inputs_changed_during_provider_call(
    monkeypatch, tmp_path
):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile, StageName, StageStatus, Summary
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_mind_map_only

    init_db()
    _seed(audio_path=None)
    _install_llm_fakes(monkeypatch, {"sum": 0, "mm": 0, "emb": 0})

    def mutate_note_then_return(_transcript, _settings, summary_md=None):
        assert summary_md == "# Sync notes\n\n- agenda\n- decisions"
        with session_scope() as s:
            note = s.scalars(
                select(Summary).where(
                    Summary.file_id == "r1", Summary.template == "default"
                )
            ).one()
            note.content_md = "# Changed while provider was running"
        return {
            "template": "mind_map",
            "content_md": "# Must not become current",
            "provider": "fake",
            "model": "m",
        }

    monkeypatch.setattr(
        "localplaud.worker.pipeline.mindmap.generate_mind_map", mutate_note_then_return
    )
    process_mind_map_only("r1")

    with session_scope() as s:
        recording = s.get(PlaudFile, "r1")
        live_map = s.scalars(
            select(Summary).where(Summary.file_id == "r1", Summary.template == "mind_map")
        ).one()
        run = next(item for item in recording.stage_runs if item.stage == StageName.mind_map)
        assert live_map.content_md == "# Sync topics\n- agenda\n  - budget"
        assert run.status == StageStatus.failed
        assert (run.detail or {}).get("stale") is True
        assert "inputs changed during rebuild" in run.error


def test_rebuild_compare_and_set_rejects_source_stage_made_stale(monkeypatch, tmp_path):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile, StageName, StageStatus, Summary
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_mind_map_only

    init_db()
    _seed(audio_path=None)
    _install_llm_fakes(monkeypatch, {"sum": 0, "mm": 0, "emb": 0})

    def make_notes_stale(_transcript, _settings, summary_md=None):
        with session_scope() as s:
            recording = s.get(PlaudFile, "r1")
            recording.note_template_key = "meeting"
            summarize = next(
                item for item in recording.stage_runs if item.stage == StageName.summarize
            )
            summarize.status = StageStatus.pending
            summarize.detail = dict(summarize.detail or {}) | {"stale": True}
        return {
            "template": "mind_map",
            "content_md": "# Must not become current",
            "provider": "fake",
            "model": "m",
        }

    monkeypatch.setattr(
        "localplaud.worker.pipeline.mindmap.generate_mind_map", make_notes_stale
    )
    process_mind_map_only("r1")

    with session_scope() as s:
        recording = s.get(PlaudFile, "r1")
        live_map = s.scalars(
            select(Summary).where(Summary.file_id == "r1", Summary.template == "mind_map")
        ).one()
        map_run = next(
            item for item in recording.stage_runs if item.stage == StageName.mind_map
        )
        assert live_map.content_md == "# Sync topics\n- agenda\n  - budget"
        assert recording.status.value == "partial"
        assert map_run.status == StageStatus.failed
        assert (map_run.detail or {}).get("stale") is True


def test_rebuild_completion_cannot_overwrite_a_post_persist_stale_write(
    monkeypatch, tmp_path
):
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile, StageName, StageStatus, Summary
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker import pipeline

    init_db()
    _seed(audio_path=None)
    _install_llm_fakes(monkeypatch, {"sum": 0, "mm": 0, "emb": 0})
    original_persist = pipeline._persist_summary

    def persist_then_commit_late_note_restore(*args, **kwargs):
        original_persist(*args, **kwargs)
        result = args[1]
        if result.get("template") != "mind_map":
            return
        with session_scope() as s:
            note = s.scalars(
                select(Summary).where(
                    Summary.file_id == "r1", Summary.template == "default"
                )
            ).one()
            note.content_md = "# Restored after map persistence"
            recording = s.get(PlaudFile, "r1")
            run = next(
                item for item in recording.stage_runs if item.stage == StageName.mind_map
            )
            run.status = StageStatus.pending
            run.detail = dict(run.detail or {}) | {
                "stale": True,
                "stale_generation": "late-note-restore",
                "reason": "note version restored",
            }

    monkeypatch.setattr(pipeline, "_persist_summary", persist_then_commit_late_note_restore)
    pipeline.process_mind_map_only("r1")

    with session_scope() as s:
        recording = s.get(PlaudFile, "r1")
        run = next(item for item in recording.stage_runs if item.stage == StageName.mind_map)
        assert recording.status.value == "partial"
        assert run.status == StageStatus.failed
        assert run.detail.get("stale") is True
        assert run.detail.get("stale_generation") == "late-note-restore"
        assert "inputs changed after generation" in run.error


def test_rebuild_failure_keeps_stale_state_and_schedules_scoped_retry(monkeypatch, tmp_path):
    """A failed rebuild keeps the old map archived-safe and hidden (stale),
    records an actionable error on the stage and the file, and the background
    scanner retries at the same mind-map-only scope without touching notes or
    the index."""
    _reset_db(monkeypatch, tmp_path)
    from datetime import UTC, datetime, timedelta

    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageStatus,
        Summary,
    )
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_mind_map_only, process_pending

    init_db()
    # The stale detail carries the route's scope marker, as a real queued
    # rebuild would.
    _seed(
        audio_path=None,
        stale_detail={
            "stale": True,
            "reason": "user requested mind map rebuild",
            "mind_map_only": True,
        },
    )
    counters = _install_llm_fakes(monkeypatch, {"sum": 0, "mm": 0, "emb": 0})

    def broken_mindmap(transcript, settings, summary_md=None):
        raise RuntimeError("boom from provider")

    monkeypatch.setattr(
        "localplaud.worker.pipeline.mindmap.generate_mind_map", broken_mindmap
    )
    unrelated_due = datetime.now(UTC) + timedelta(hours=4)
    with session_scope() as s:
        recording = s.get(PlaudFile, "r1")
        recording.pipeline_retry_count = 2
        recording.pipeline_next_retry_at = unrelated_due
        before = _snapshot_state(s)

    process_mind_map_only("r1")

    with session_scope() as s:
        assert _snapshot_state(s) == before
        recording = s.get(PlaudFile, "r1")
        assert recording.status == FileStatus.partial
        assert "mind_map: " in recording.error and "boom from provider" in recording.error
        # Mind-map retry state is stage-scoped; unrelated file-level backoff
        # remains exactly untouched.
        assert recording.pipeline_retry_count == 2
        assert recording.pipeline_next_retry_at.replace(tzinfo=UTC) == unrelated_due
        assert recording.processing_token is None
        run = next(r for r in recording.stage_runs if r.stage == StageName.mind_map)
        assert run.status == StageStatus.failed
        assert "boom from provider" in run.error
        # The stale flag and the narrow retry scope both survive the failure.
        assert run.detail.get("stale") is True
        assert run.detail.get("mind_map_only") is True
        assert run.detail.get("reason") == "user requested mind map rebuild"
        assert run.detail.get("mind_map_retry_count") == 1
        assert run.detail.get("mind_map_next_retry_at") is not None
        # The outdated artifact row itself is preserved.
        old_map = s.scalars(
            select(Summary).where(Summary.file_id == "r1", Summary.template == "mind_map")
        ).one()
        assert old_map.content_md == "# Sync topics\n- agenda\n  - budget"

    # The scanner resumes this recording at mind-map-only scope: the fixed
    # provider rebuilds the map; notes and index are still never executed.
    monkeypatch.undo()  # restore the working fake installed above
    counters = _install_llm_fakes(monkeypatch, {"sum": 0, "mm": 0, "emb": 0})
    with session_scope() as s:
        recording = s.get(PlaudFile, "r1")
        run = next(r for r in recording.stage_runs if r.stage == StageName.mind_map)
        detail = dict(run.detail)
        detail["mind_map_next_retry_at"] = (
            datetime.now(UTC) - timedelta(seconds=1)
        ).isoformat()
        run.detail = detail
    assert process_pending() == 1
    assert counters["sum"] == 0 and counters["emb"] == 0 and counters["mm"] == 1
    with session_scope() as s:
        recording = s.get(PlaudFile, "r1")
        assert recording.status == FileStatus.done and recording.error is None
        assert recording.pipeline_retry_count == 2
        assert recording.pipeline_next_retry_at.replace(tzinfo=UTC) == unrelated_due
        run = next(r for r in recording.stage_runs if r.stage == StageName.mind_map)
        assert run.status == StageStatus.completed
        assert not (run.detail or {}).get("stale")


def test_rebuild_setup_guards(monkeypatch, tmp_path):
    """Setup preconditions fail loudly without inventing stage state: stale
    notes, no note output, and no canonical local transcript each refuse, and
    the failure is recorded on the file for retry."""
    import pytest

    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import (
        FileStatus,
        PlaudFile,
        StageName,
        StageRun,
        StageStatus,
        Summary,
    )
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_mind_map_only

    init_db()
    _seed(audio_path=None)
    _install_llm_fakes(monkeypatch, {"sum": 0, "mm": 0, "emb": 0})

    with session_scope() as s:
        summarize = s.scalars(
            select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.summarize
            )
        ).one()
        summarize.detail = {"sentinel": "summarize", "stale": True}
        summarize.status = StageStatus.pending
    with pytest.raises(ValueError, match="regenerate notes"):
        process_mind_map_only("r1")
    with session_scope() as s:
        recording = s.get(PlaudFile, "r1")
        assert recording.status == FileStatus.error
        assert "regenerate notes" in recording.error
        assert recording.processing_token is None

    with session_scope() as s:
        summarize = s.scalars(
            select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.summarize
            )
        ).one()
        summarize.detail = {"sentinel": "summarize"}
        summarize.status = StageStatus.completed
        recording = s.get(PlaudFile, "r1")
        recording.status = FileStatus.done
        recording.error = None
        for note in list(
            s.scalars(
                select(Summary).where(
                    Summary.file_id == "r1", Summary.template != "mind_map"
                )
            )
        ):
            s.delete(note)
    with pytest.raises(ValueError, match="note output is required"):
        process_mind_map_only("r1")


def test_rebuild_source_selection(monkeypatch, tmp_path):
    """Source note precedence: the map's recorded source template wins, then
    the recording's configured template, then the workspace default; ``auto``
    only resolves through a single unambiguous live output."""
    _reset_db(monkeypatch, tmp_path)
    from localplaud.config import get_settings
    from localplaud.db.models import PlaudFile, Summary
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import mind_map_rebuild_source

    init_db()
    _seed(audio_path=None)
    settings = get_settings()
    with session_scope() as s:
        row = s.get(PlaudFile, "r1")
        # Recorded source template wins.
        assert mind_map_rebuild_source(s, row, settings).template == "default"

        # Without a recorded source, the recording's configured template wins.
        live_map = next(x for x in row.summaries if x.template == "mind_map")
        live_map.template_snapshot = {}
        s.add(
            Summary(
                file_id="r1",
                template="meeting",
                title="Meeting",
                content_md="# meeting",
                source="local",
                template_version=1,
            )
        )
        s.flush()
        s.expire(row)
        row.note_template_key = "meeting"
        assert mind_map_rebuild_source(s, row, settings).template == "meeting"

        # ``auto`` falls through to the workspace default template.
        row.note_template_key = "auto"
        assert mind_map_rebuild_source(s, row, settings).template == "default"

        # Ambiguous: several live outputs and nothing names one of them.
        for note in row.summaries:
            if note.template == "default":
                note.template = "class"
        s.flush()
        s.expire(row)
        assert mind_map_rebuild_source(s, row, settings) is None

        # A single live output is unambiguous even under ``auto``.
        for note in list(row.summaries):
            if note.template == "class":
                s.delete(note)
        s.flush()
        s.expire(row)
        assert mind_map_rebuild_source(s, row, settings).template == "meeting"

        # No live local note output at all.
        for note in list(row.summaries):
            if note.template != "mind_map":
                s.delete(note)
        s.flush()
        s.expire(row)
        assert mind_map_rebuild_source(s, row, settings) is None


def test_successful_rebuild_does_not_mask_other_stage_failures(monkeypatch, tmp_path):
    """A clean mind-map cycle must not roll the file up to done while another
    stage still carries a recorded failure the rebuild never touched."""
    _reset_db(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import init_db, session_scope
    from localplaud.worker.pipeline import process_mind_map_only

    init_db()
    _seed(audio_path=None)
    _install_llm_fakes(monkeypatch, {"sum": 0, "mm": 0, "emb": 0})
    from datetime import UTC, datetime, timedelta

    unrelated_due = datetime.now(UTC) + timedelta(hours=2)
    with session_scope() as s:
        recording = s.get(PlaudFile, "r1")
        recording.pipeline_retry_count = 2
        recording.pipeline_next_retry_at = unrelated_due
        index_run = s.scalars(
            select(StageRun).where(StageRun.file_id == "r1", StageRun.stage == StageName.index)
        ).one()
        index_run.status = StageStatus.failed
        index_run.error = "embedding provider unreachable"

    process_mind_map_only("r1")

    with session_scope() as s:
        recording = s.get(PlaudFile, "r1")
        assert recording.status == FileStatus.partial
        assert "index: embedding provider unreachable" in recording.error
        assert recording.pipeline_retry_count == 2
        assert recording.pipeline_next_retry_at.replace(tzinfo=UTC) == unrelated_due
        run = next(r for r in recording.stage_runs if r.stage == StageName.mind_map)
        assert run.status == StageStatus.completed


# ---------------------------------------------------------------------------
# API route


def _deferred_threads(monkeypatch):
    started = []

    class DeferredThread:
        def __init__(self, **kwargs):
            started.append(kwargs)

        def start(self):
            pass

    monkeypatch.setattr("threading.Thread", DeferredThread)
    return started


def test_rebuild_route_validations(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from datetime import UTC, datetime, timedelta

    from localplaud.db.models import PlaudFile, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope

    assert c.post("/file/missing/rebuild-mind-map").status_code == 404

    _seed(with_transcript=False, audio_path=None)
    denied = c.post("/file/r1/rebuild-mind-map")
    assert denied.status_code == 409
    assert "local transcript" in denied.text

    with session_scope() as s:
        from localplaud.db.models import Transcript

        s.add(
            Transcript(
                file_id="r1",
                provider="fake-asr",
                model="fake-model",
                language="en",
                has_speakers=True,
                source="local",
                text="hello team",
                segments=SEGMENTS,
            )
        )
        row = s.get(PlaudFile, "r1")
        row.processing_token = "tok"
        row.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)
    busy = c.post("/file/r1/rebuild-mind-map")
    assert busy.status_code == 409 and "already processing" in busy.text

    with session_scope() as s:
        row = s.get(PlaudFile, "r1")
        row.processing_token = None
        row.processing_lease_until = None
        summarize = s.scalars(
            select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.summarize
            )
        ).one()
        summarize.detail = {"stale": True}
        summarize.status = StageStatus.pending
    stale_notes = c.post("/file/r1/rebuild-mind-map")
    assert stale_notes.status_code == 409
    assert "regenerate notes instead" in stale_notes.text

    with session_scope() as s:
        summarize = s.scalars(
            select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.summarize
            )
        ).one()
        summarize.detail = {}
        summarize.status = StageStatus.completed
        from localplaud.db.models import Summary

        for note in list(
            s.scalars(select(Summary).where(Summary.file_id == "r1", Summary.template != "mind_map"))
        ):
            s.delete(note)
    no_notes = c.post("/file/r1/rebuild-mind-map")
    assert no_notes.status_code == 409
    assert "generated notes are required first" in no_notes.text


def test_active_rebuild_claim_blocks_every_web_input_mutation(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from datetime import UTC, datetime, timedelta

    from localplaud.db.models import PlaudFile, Summary
    from localplaud.db.session import session_scope

    _seed(audio_path=None)
    with session_scope() as s:
        row = s.get(PlaudFile, "r1")
        row.processing_token = "mind-map-claim"
        row.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)
        note_id = s.scalars(
            select(Summary).where(Summary.file_id == "r1", Summary.template == "default")
        ).one().id

    responses = [
        c.post(
            "/file/r1/speakers",
            data={"key": "SPEAKER_00", "name": "Alice", "return_to": "/"},
        ),
        c.post("/file/r1/transcript/segments/0", data={"text": "changed", "base_revision": 0}),
        c.post(
            "/file/r1/transcript/replace",
            data={"find": "hello", "replace": "changed", "base_revision": 0},
        ),
        c.post("/file/r1/transcript/revisions/1/restore", data={"base_revision": 0}),
        c.post(f"/file/r1/summaries/{note_id}/versions/1/restore", data={"tab": "notes"}),
        c.put("/api/files/r1/note-template", json={"key": "auto"}),
        c.post("/api/vocabulary/apply-library"),
    ]
    assert [response.status_code for response in responses] == [409] * len(responses)


def test_rebuild_route_queues_only_the_mind_map_stage(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from datetime import UTC, datetime, timedelta

    from localplaud.db.models import FileStatus, PlaudFile, StageName
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import process_mind_map_only

    _seed(audio_path=None, stale_detail={"stale": True, "reason": "note version restored"})
    unrelated_due = datetime.now(UTC) + timedelta(hours=2)
    with session_scope() as s:
        recording = s.get(PlaudFile, "r1")
        recording.pipeline_retry_count = 2
        recording.pipeline_next_retry_at = unrelated_due
    started = _deferred_threads(monkeypatch)

    queued = c.post("/file/r1/rebuild-mind-map")
    assert queued.status_code == 200
    assert "mind map rebuild queued" in queued.text
    assert len(started) == 1
    assert started[0]["target"] is process_mind_map_only
    assert started[0]["args"] == ("r1",)
    repeated = c.post("/file/r1/rebuild-mind-map")
    assert repeated.status_code == 409
    assert "already processing" in repeated.text
    assert len(started) == 1

    with session_scope() as s:
        recording = s.get(PlaudFile, "r1")
        assert recording.status == FileStatus.processing
        assert recording.pipeline_retry_count == 2
        assert recording.pipeline_next_retry_at.replace(tzinfo=UTC) == unrelated_due
        assert recording.processing_token is not None
        assert started[0]["kwargs"]["claim_token"] == recording.processing_token
        runs = {run.stage: run for run in recording.stage_runs}
        map_detail = runs[StageName.mind_map].detail
        assert map_detail.get("stale") is True
        assert map_detail.get("mind_map_only") is True
        assert map_detail.get("reason") == "user requested mind map rebuild"
        assert "derived_only" not in map_detail
        # The other stages keep their completed state and detail verbatim.
        assert runs[StageName.summarize].detail == {"sentinel": "summarize"}
        assert runs[StageName.index].detail == {"sentinel": "index"}
        assert runs[StageName.summarize].status.value == "completed"
        assert runs[StageName.index].status.value == "completed"


def test_rebuild_route_rejects_a_current_map(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed(audio_path=None, map_stale=False)
    started = _deferred_threads(monkeypatch)

    response = c.post("/file/r1/rebuild-mind-map")
    assert response.status_code == 409
    assert "already current" in response.text
    assert started == []


def test_rebuild_route_keeps_durable_queue_when_thread_start_fails(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, StageName
    from localplaud.db.session import session_scope

    _seed(
        audio_path=None,
        stale_detail={
            "stale": True,
            "mind_map_only": True,
            "mind_map_retry_count": 5,
            "mind_map_next_retry_at": None,
        },
    )

    class BrokenThread:
        def __init__(self, **_kwargs):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr("threading.Thread", BrokenThread)
    response = c.post("/file/r1/rebuild-mind-map")
    assert response.status_code == 503
    assert "remains queued" in response.text
    with session_scope() as s:
        row = s.get(PlaudFile, "r1")
        run = next(item for item in row.stage_runs if item.stage == StageName.mind_map)
        assert row.status == FileStatus.partial
        assert row.processing_token is None
        assert run.detail.get("mind_map_only") is True
        assert run.detail.get("stale") is True
        assert "mind_map_retry_count" not in run.detail
        assert "mind_map_next_retry_at" not in run.detail

    processed = []
    monkeypatch.setattr(
        "localplaud.worker.pipeline.process_mind_map_only",
        lambda file_id, settings: processed.append(file_id),
    )
    from localplaud.worker.pipeline import process_pending

    assert process_pending() == 1
    assert processed == ["r1"]


def test_rebuild_route_revalidates_current_state_after_claim(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile, StageName, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker import pipeline

    _seed(audio_path=None)
    original_claim = pipeline.claim_mind_map_rebuild

    def finish_first_request_then_claim(file_id):
        with session_scope() as s:
            row = s.get(PlaudFile, file_id)
            run = next(item for item in row.stage_runs if item.stage == StageName.mind_map)
            run.status = StageStatus.completed
            run.detail = {}
        token = original_claim(file_id)
        from localplaud.poller.poll import reset_inflight

        # The handoff keeps the poller's invariant: every live claim is visibly
        # processing, so ordinary restart cleanup cannot orphan it.
        assert reset_inflight() == 0
        with session_scope() as s:
            assert s.get(PlaudFile, file_id).processing_token == token
        return token

    monkeypatch.setattr(
        pipeline, "claim_mind_map_rebuild", finish_first_request_then_claim
    )
    started = _deferred_threads(monkeypatch)
    response = c.post("/file/r1/rebuild-mind-map")
    assert response.status_code == 409
    assert "already current" in response.text
    assert started == []
    with session_scope() as s:
        row = s.get(PlaudFile, "r1")
        run = next(item for item in row.stage_runs if item.stage == StageName.mind_map)
        assert row.processing_token is None
        assert run.status == StageStatus.completed
        assert not (run.detail or {}).get("stale")


def test_scope_markers_supersede_each_other(monkeypatch, tmp_path):
    """A full regeneration clears the narrow rebuild marker and vice versa, so
    the background scanner always retries at the most recently requested
    scope."""
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageRun
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import release_processing_claim

    _seed(audio_path=None)
    _deferred_threads(monkeypatch)

    assert c.post("/file/r1/rebuild-mind-map").status_code == 200
    with session_scope() as s:
        detail = s.scalars(
            select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.mind_map
            )
        ).one().detail
        assert detail.get("mind_map_only") is True and "derived_only" not in detail

    # The synchronous lease makes a broader request conflict while this
    # operation is queued, rather than racing two background threads.
    assert c.post("/file/r1/generate-notes").status_code == 409
    with session_scope() as s:
        row = s.get(PlaudFile, "r1")
        token = row.processing_token
    release_processing_claim("r1", token)
    with session_scope() as s:
        s.get(PlaudFile, "r1").status = FileStatus.partial

    assert c.post("/file/r1/generate-notes").status_code == 200
    with session_scope() as s:
        detail = s.scalars(
            select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.mind_map
            )
        ).one().detail
        assert detail.get("derived_only") is True and "mind_map_only" not in detail

    assert c.post("/file/r1/rebuild-mind-map").status_code == 409  # notes now stale


# ---------------------------------------------------------------------------
# Workspace UI states


def test_mindmap_tab_offers_rebuild_when_inputs_are_current(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed(audio_path=None)
    page = c.get("/file/r1?tab=mindmap")
    assert "Mind map is out of date." in page.text
    assert 'id="rebuild-mindmap"' in page.text
    assert "Rebuild it from the current notes" in page.text
    assert "Rebuild mind map" in page.text
    assert "The last rebuild failed:" not in page.text
    assert 'id="mindmap-src"' not in page.text
    # The rebuild endpoint is wired to the button handler.
    assert "/file/r1/rebuild-mind-map" in page.text


def test_mindmap_tab_falls_back_to_regenerate_when_notes_stale(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope

    _seed(audio_path=None)
    with session_scope() as s:
        summarize = s.scalars(
            select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.summarize
            )
        ).one()
        summarize.detail = {"stale": True}
        summarize.status = StageStatus.pending
    page = c.get("/file/r1?tab=mindmap")
    assert "Mind map is out of date." in page.text
    assert 'id="rebuild-mindmap"' not in page.text
    assert "Regenerate notes to rebuild it" in page.text


def test_mindmap_tab_shows_actionable_failure_and_retry(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import StageName, StageRun
    from localplaud.db.session import session_scope

    _seed(audio_path=None)
    with session_scope() as s:
        run = s.scalars(
            select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.mind_map
            )
        ).one()
        from localplaud.db.models import StageStatus

        run.status = StageStatus.failed
        run.error = "boom from provider"
        run.detail = dict(run.detail or {}) | {"mind_map_only": True}
    page = c.get("/file/r1?tab=mindmap")
    assert "The last rebuild failed:" in page.text
    assert "boom from provider" in page.text
    assert "Try the rebuild again" in page.text
    assert 'id="rebuild-mindmap"' in page.text


def test_mindmap_tab_shows_progress_while_processing(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from datetime import UTC, datetime, timedelta

    from localplaud.db.models import FileStatus, PlaudFile, StageName
    from localplaud.db.session import session_scope

    _seed(audio_path=None)
    with session_scope() as s:
        row = s.get(PlaudFile, "r1")
        row.status = FileStatus.processing
        row.processing_token = "mind-map-claim"
        row.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)
        run = next(item for item in row.stage_runs if item.stage == StageName.mind_map)
        run.detail = dict(run.detail or {}) | {"mind_map_only": True}
    page = c.get("/file/r1?tab=mindmap")
    assert "Rebuilding mind map…" in page.text
    assert "data-generation-progress" in page.text
    assert "data-static-title" in page.text
    assert 'id="rebuild-mindmap"' not in page.text


def test_mindmap_tab_does_not_mislabel_unrelated_processing(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from datetime import UTC, datetime, timedelta

    from localplaud.db.models import FileStatus, PlaudFile, StageName, StageStatus
    from localplaud.db.session import session_scope

    _seed(audio_path=None)
    with session_scope() as s:
        row = s.get(PlaudFile, "r1")
        row.status = FileStatus.processing
        row.processing_token = "index-claim"
        row.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)
        index_run = next(item for item in row.stage_runs if item.stage == StageName.index)
        index_run.status = StageStatus.running
    page = c.get("/file/r1?tab=mindmap")
    assert "Rebuilding mind map…" not in page.text
    assert 'id="rebuild-mindmap"' in page.text


def test_export_recovers_after_successful_rebuild(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.worker.pipeline import process_mind_map_only

    _seed(audio_path=None)
    _install_llm_fakes(monkeypatch, {"sum": 0, "mm": 0, "emb": 0})
    denied = c.get("/file/r1/export/mind-map.png")
    assert denied.status_code == 409
    assert "out of date" in denied.json()["detail"]

    process_mind_map_only("r1")

    exported = c.get("/file/r1/export/mind-map.png")
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "image/png"
    page = c.get("/file/r1?tab=mindmap")
    assert 'id="mindmap-src"' in page.text
    assert "Rebuilt" in page.text


# ---------------------------------------------------------------------------
# Restore refinement: restoring the exact content the map was built from


def test_restoring_the_maps_recorded_source_content_keeps_it_current(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    from localplaud.db.models import StageName, StageRun, StageStatus, Summary
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _persist_summary, process_mind_map_only

    _seed(audio_path=None)
    _install_llm_fakes(monkeypatch, {"sum": 0, "mm": 0, "emb": 0})
    # v2 displaces the seeded v1 note (archived as revision 1).
    _persist_summary(
        "r1",
        {
            "template": "default",
            "title": "Sync notes",
            "content_md": "# Sync notes v2",
            "provider": "fake-llm",
            "model": "m-1",
            "template_version": 1,
            "template_snapshot": {"name": "Default", "version": 1},
        },
        dict(LINEAGE),
    )
    # Rebuild records the fingerprint of the live v2 content.
    process_mind_map_only("r1")
    with session_scope() as s:
        note_id = s.scalars(
            select(Summary).where(Summary.file_id == "r1", Summary.template == "default")
        ).one().id

    def map_run_state():
        with session_scope() as s:
            run = s.scalars(
                select(StageRun).where(
                    StageRun.file_id == "r1", StageRun.stage == StageName.mind_map
                )
            ).one()
            return run.status, (run.detail or {}).get("stale")

    assert map_run_state() == (StageStatus.completed, None)

    # Restoring v1 changes the map's input: it goes out of date.
    assert (
        c.post(
            f"/file/r1/summaries/{note_id}/versions/1/restore",
            data={"tab": "notes"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    status, stale = map_run_state()
    assert stale is True

    # Rebuild from v1, then restore v1's twin revision again: the recorded
    # fingerprint proves the input is unchanged, so the map stays current.
    process_mind_map_only("r1")
    assert map_run_state() == (StageStatus.completed, None)
    # Find the archived revision whose content equals the live (v1) content —
    # restoring it is an input no-op for the map.
    with session_scope() as s:
        from localplaud.db.models import SummaryRevision

        live_content = s.scalars(
            select(Summary).where(Summary.file_id == "r1", Summary.template == "default")
        ).one().content_md
        twin_revision = next(
            r.revision
            for r in s.scalars(
                select(SummaryRevision).where(
                    SummaryRevision.file_id == "r1", SummaryRevision.template == "default"
                )
            )
            if r.content_md == live_content
        )
    assert (
        c.post(
            f"/file/r1/summaries/{note_id}/versions/{twin_revision}/restore",
            data={"tab": "notes"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert map_run_state() == (StageStatus.completed, None)
