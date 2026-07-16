"""Generated-note version history: every displacement of a live generated
Summary — regeneration or an explicit restore — preserves an immutable,
provenance-carrying SummaryRevision, scoped per (file, template). Restore is a
content swap that queues nothing; a mind map sourced from the restored note
output is marked out of date (stale) because its input changed."""

from __future__ import annotations

from sqlalchemy import create_engine, select, text

LINEAGE = {
    "input_transcript_id": 1,
    "input_transcript_revision": 0,
    "input_transcript_source": "local",
}


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'history.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


SEGMENTS = [
    {"text": "hello team", "start": 1.0, "end": 2.0, "speaker": "SPEAKER_00", "words": []},
    {"text": "let's start", "start": 2.0, "end": 3.0, "speaker": "SPEAKER_01", "words": []},
]


def _seed(with_index: bool = False):
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

    with session_scope() as s:
        s.add(PlaudFile(id="r1", filename="Weekly Sync", status=FileStatus.done,
                        duration_ms=600000, start_time_ms=1783582737000))
        s.add(Transcript(file_id="r1", provider="faster-whisper", model="large-v3-turbo",
                         language="en", has_speakers=True, source="local",
                         text="hello team\nlet's start", segments=SEGMENTS))
        if with_index:
            s.add(Chunk(file_id="r1", idx=0, text="hello team let's start",
                        start=1.0, end=3.0))
            s.add(StageRun(file_id="r1", stage=StageName.index,
                           status=StageStatus.completed, attempts=1))
            s.add(StageRun(file_id="r1", stage=StageName.summarize,
                           status=StageStatus.completed, attempts=1))


_UNSET = object()


def _generate(content: str, template: str = "default", *, title: str | None = None,
              template_version: int | None = 1, snapshot=_UNSET):
    from localplaud.worker.pipeline import _persist_summary

    if snapshot is _UNSET:
        snapshot = {"name": template.title(), "version": template_version}
    _persist_summary(
        "r1",
        {
            "template": template,
            "title": title or f"{template} title",
            "content_md": content,
            "provider": "fake-llm",
            "model": "m-1",
            "template_version": template_version,
            "template_snapshot": snapshot,
        },
        dict(LINEAGE),
    )


def _live(session, template: str = "default"):
    from localplaud.db.models import Summary

    return session.scalars(
        select(Summary).where(Summary.file_id == "r1", Summary.template == template)
    ).one()


def _revisions(session, template: str = "default"):
    from localplaud.db.models import SummaryRevision

    return list(
        session.scalars(
            select(SummaryRevision)
            .where(SummaryRevision.file_id == "r1", SummaryRevision.template == template)
            .order_by(SummaryRevision.revision)
        )
    )


def test_summary_revision_migration_is_idempotent(tmp_path):
    from localplaud.db.migrations import migrate_summary_revision_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-notes.db'}")
    with engine.begin() as connection:
        # The pre-m8 production layout: provenance columns exist, archival ones don't.
        connection.execute(text("""
            CREATE TABLE summary_revisions (
                id INTEGER PRIMARY KEY,
                file_id VARCHAR(64) NOT NULL,
                template VARCHAR(64) NOT NULL,
                revision INTEGER NOT NULL,
                title VARCHAR(512),
                content_md TEXT NOT NULL,
                llm_provider VARCHAR(64),
                model VARCHAR(128),
                source VARCHAR(16) NOT NULL,
                template_version INTEGER,
                transcript_revision INTEGER,
                profile_snapshot JSON NOT NULL,
                created_at DATETIME
            )
        """))
        connection.execute(text(
            "CREATE TABLE summaries (id INTEGER PRIMARY KEY, template VARCHAR(64))"
        ))
        connection.execute(text("""
            INSERT INTO summary_revisions
                (file_id, template, revision, content_md, source, profile_snapshot)
            VALUES ('recording', 'default', 1, 'legacy content', 'local', '{}')
        """))
    assert set(migrate_summary_revision_schema(engine)) == {
        "summary_revisions.template_snapshot",
        "summary_revisions.input_transcript_id",
        "summary_revisions.input_transcript_source",
        "summary_revisions.archived_at",
        "summary_revisions.archive_reason",
        "summaries.restored_from_revision",
    }
    assert migrate_summary_revision_schema(engine) == []
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT content_md, archive_reason, archived_at FROM summary_revisions"
        )).one()
    assert row.content_md == "legacy content"
    assert row.archive_reason is None and row.archived_at is None


def test_regeneration_archives_prior_version_with_provenance(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.session import session_scope

    _generate("# v1 body", title="First pass", template_version=3)
    with session_scope() as s:
        first = _live(s)
        first_created = first.created_at
        assert _revisions(s) == []  # first generation displaces nothing

    _generate("# v2 body", title="Second pass", template_version=4)
    with session_scope() as s:
        live = _live(s)
        assert live.content_md == "# v2 body" and live.template_version == 4
        revs = _revisions(s)
        assert len(revs) == 1
        archived = revs[0]
        assert archived.revision == 1
        assert archived.content_md == "# v1 body"
        assert archived.title == "First pass"
        assert archived.llm_provider == "fake-llm" and archived.model == "m-1"
        assert archived.source == "local"
        assert archived.template_version == 3
        assert archived.template_snapshot == {"name": "Default", "version": 3}
        assert archived.input_transcript_revision == 0
        assert archived.input_transcript_source == "local"
        assert archived.resolved_profile_snapshot == {}
        assert archived.created_at == first_created  # original generation time
        assert archived.archived_at is not None
        assert archived.archive_reason == "regenerated"


def test_identical_regeneration_creates_no_duplicate_version(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.session import session_scope

    _generate("# same body")
    _generate("# same body")  # identical rerun: content stays live, nothing to preserve
    with session_scope() as s:
        assert _revisions(s) == []

    _generate("# changed body")
    _generate("# changed body")  # identical rerun after a real change
    with session_scope() as s:
        revs = _revisions(s)
        assert [(r.revision, r.content_md) for r in revs] == [(1, "# same body")]

    # A -> B -> A: the returning content is a new displacement, not a duplicate.
    _generate("# same body")
    with session_scope() as s:
        revs = _revisions(s)
        assert [(r.revision, r.content_md) for r in revs] == [
            (1, "# same body"),
            (2, "# changed body"),
        ]
        assert _live(s).content_md == "# same body"


def test_template_chains_are_independent(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.session import session_scope

    for template in ("default", "meeting", "mind_map"):
        _generate(f"# {template} v1", template=template)
        _generate(f"# {template} v2", template=template)
    with session_scope() as s:
        for template in ("default", "meeting", "mind_map"):
            revs = _revisions(s, template)
            assert [(r.revision, r.content_md) for r in revs] == [(1, f"# {template} v1")]
        meeting_id = _live(s, "meeting").id
    history = c.get(f"/api/files/r1/summaries/{meeting_id}/history").json()
    assert history["template"] == "meeting"
    assert [v["content_md"] for v in history["versions"]] == ["# meeting v1"]


def test_cloud_summaries_never_enter_history(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import Summary, SummaryRevision
    from localplaud.db.session import session_scope
    from localplaud.note_history import archive_summary

    with session_scope() as s:
        s.add(Summary(file_id="r1", template="plaud", source="cloud",
                      content_md="mirrored cloud summary"))
    with session_scope() as s:
        cloud = s.scalars(select(Summary).where(Summary.template == "plaud")).one()
        assert archive_summary(s, cloud, reason="regenerated") is None
        cloud_id = cloud.id
    with session_scope() as s:
        assert list(s.scalars(select(SummaryRevision))) == []

    history = c.get(f"/api/files/r1/summaries/{cloud_id}/history").json()
    assert history["source"] == "cloud"
    assert history["version_count"] == 0 and history["versions"] == []
    denied = c.post(f"/file/r1/summaries/{cloud_id}/versions/1/restore",
                    data={"tab": "notes"}, follow_redirects=False)
    assert denied.status_code == 400
    assert "locally generated" in denied.json()["error"]


def test_restore_preserves_displaced_current_and_queues_nothing(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    from localplaud.db.models import (
        Chunk,
        FileStatus,
        PlaudFile,
        StageRun,
        TranscriptRevision,
        UserNote,
    )
    from localplaud.db.session import session_scope

    _generate("# v1 body", title="First pass", template_version=1)
    _generate("# v2 body", title="Second pass", template_version=2)
    with session_scope() as s:
        live = _live(s)
        live_id = live.id
        v2_created = live.created_at
        v1_created = _revisions(s)[0].created_at
        s.add(UserNote(file_id="r1", title="Edited copy", content_md="user copy",
                       source_summary_id=live_id))
        stage_states = {run.stage: (run.status, run.attempts) for run in
                        s.get(PlaudFile, "r1").stage_runs}

    restored = c.post(f"/file/r1/summaries/{live_id}/versions/1/restore",
                      data={"tab": "notes"}, follow_redirects=False)
    assert restored.status_code == 303
    # Redirect lands back on the exact note output that was restored.
    assert restored.headers["location"] == f"/file/r1?tab=notes&note=sum-{live_id}"

    with session_scope() as s:
        live = _live(s)
        # Same live row, updated in place: editable-copy links survive.
        assert live.id == live_id
        assert live.content_md == "# v1 body" and live.title == "First pass"
        assert live.template_version == 1
        assert live.restored_from_revision == 1
        assert live.created_at == v1_created  # truthful original generation time
        note = s.scalars(select(UserNote).where(UserNote.file_id == "r1")).one()
        assert note.source_summary_id == live_id

        revs = _revisions(s)
        assert [(r.revision, r.content_md, r.archive_reason) for r in revs] == [
            (1, "# v1 body", "regenerated"),
            (2, "# v2 body", "restore"),  # displaced current preserved first
        ]
        assert revs[1].title == "Second pass"
        assert revs[1].created_at == v2_created

        # Nothing queued, nothing invalidated: no mind map exists here, so
        # nothing depends on these notes and no stage (and no phantom
        # StageRun) may be touched. Dependent-mind-map staleness is covered
        # separately below.
        assert s.query(Chunk).filter_by(file_id="r1").count() == 1
        assert s.query(TranscriptRevision).count() == 0
        row = s.get(PlaudFile, "r1")
        assert row.status == FileStatus.done
        assert {run.stage: (run.status, run.attempts) for run in row.stage_runs} \
            == stage_states
        assert all(not (run.detail or {}).get("stale") for run in row.stage_runs)
        assert s.query(StageRun).count() == len(stage_states)

    # Restoring back is a new displacement in the same chain (no duplicates).
    back = c.post(f"/file/r1/summaries/{live_id}/versions/2/restore",
                  data={"tab": "mindmap"}, follow_redirects=False)
    assert back.status_code == 303
    assert back.headers["location"] == "/file/r1?tab=mindmap"
    with session_scope() as s:
        live = _live(s)
        assert live.content_md == "# v2 body"
        assert live.restored_from_revision == 2
        assert [(r.revision, r.content_md, r.archive_reason) for r in _revisions(s)] == [
            (1, "# v1 body", "regenerated"),
            (2, "# v2 body", "restore"),
            (3, "# v1 body", "restore"),
        ]


def test_restore_validation_stale_unknown_and_tab_sanitization(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.session import session_scope

    _generate("# v1 body")
    _generate("# v2 body")
    with session_scope() as s:
        summary_id = _live(s).id

    # Regenerated/removed since render: the id no longer matches a live row.
    stale = c.post("/file/r1/summaries/999999/versions/1/restore",
                   data={"tab": "notes"}, follow_redirects=False)
    assert stale.status_code == 409
    assert stale.json() == {"error": "notes changed; reload before restoring"}
    mismatch = c.post(f"/file/other/summaries/{summary_id}/versions/1/restore",
                      data={"tab": "notes"}, follow_redirects=False)
    assert mismatch.status_code == 409

    missing = c.post(f"/file/r1/summaries/{summary_id}/versions/99/restore",
                     data={"tab": "notes"}, follow_redirects=False)
    assert missing.status_code == 404
    assert missing.json() == {"error": "version not found"}

    evil_tab = c.post(f"/file/r1/summaries/{summary_id}/versions/1/restore",
                      data={"tab": "javascript:alert(1)"}, follow_redirects=False)
    assert evil_tab.status_code == 303
    assert evil_tab.headers["location"] == f"/file/r1?tab=notes&note=sum-{summary_id}"


def test_history_endpoint_shape_limit_and_current_flag(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.session import session_scope

    _generate("# v1 body", title="First pass")
    _generate("# v2 body", title="Second pass")
    _generate("# v3 body", title="Third pass")
    with session_scope() as s:
        summary_id = _live(s).id

    history = c.get(f"/api/files/r1/summaries/{summary_id}/history")
    assert history.status_code == 200
    body = history.json()
    assert body["file_id"] == "r1" and body["summary_id"] == summary_id
    assert body["template"] == "default" and body["source"] == "local"
    assert body["current"]["title"] == "Third pass"
    assert body["current"]["restored_from_revision"] is None
    assert body["current"]["lineage_label"] == "raw ASR"
    assert body["version_count"] == 2
    assert [v["revision"] for v in body["versions"]] == [2, 1]  # newest first
    for version in body["versions"]:
        assert version["archive_reason"] == "regenerated"
        assert version["archived_at"] and version["created_at"]
        assert version["lineage_label"] == "raw ASR"
        assert version["is_current"] is False

    clamped = c.get(f"/api/files/r1/summaries/{summary_id}/history?limit=1").json()
    assert clamped["version_count"] == 2
    assert [v["revision"] for v in clamped["versions"]] == [2]

    # After a restore, the archived source version reads as the current content.
    c.post(f"/file/r1/summaries/{summary_id}/versions/1/restore",
           data={"tab": "notes"}, follow_redirects=False)
    restored = c.get(f"/api/files/r1/summaries/{summary_id}/history").json()
    assert restored["version_count"] == 3
    assert restored["current"]["restored_from_revision"] == 1
    flags = {v["revision"]: v["is_current"] for v in restored["versions"]}
    assert flags == {1: True, 2: False, 3: False}

    assert c.get("/api/files/r1/summaries/999999/history").status_code == 404
    assert c.get(f"/api/files/other/summaries/{summary_id}/history").status_code == 404


def test_detail_page_history_control_without_dead_restore_buttons(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.session import session_scope

    page = c.get("/file/r1")
    assert 'class="note-history-tool"' not in page.text

    _generate("# note v1")
    _generate("# note v2")
    _generate("# map v1", template="mind_map")
    _generate("# map v2", template="mind_map")
    with session_scope() as s:
        note_id = _live(s).id
        map_id = _live(s, "mind_map").id

    page = c.get("/file/r1")
    assert "Version history" in page.text
    assert f"/file/r1/summaries/{note_id}/versions/1/restore" in page.text
    assert f"/file/r1/summaries/{map_id}/versions/1/restore" in page.text
    assert 'name="tab" value="notes"' in page.text
    assert 'name="tab" value="mindmap"' in page.text
    assert "Nothing is queued or regenerated." in page.text
    assert "replaced by regeneration" in page.text
    assert "note v1" in page.text  # archived content rendered for preview
    # tojson does not escape double quotes; inside a double-quoted onsubmit
    # attribute the raw output breaks the handler and the confirm guard is
    # silently skipped. The rendered attribute must carry the escaped form.
    assert 'confirm("' not in page.text
    assert "confirm(&#34;" in page.text

    c.post(f"/file/r1/summaries/{note_id}/versions/1/restore",
           data={"tab": "notes"}, follow_redirects=False)
    page = c.get("/file/r1")
    # The restored version is current: no dead Restore button for it.
    assert f"/file/r1/summaries/{note_id}/versions/1/restore" not in page.text
    assert f"/file/r1/summaries/{note_id}/versions/2/restore" in page.text
    assert "restored from version" in page.text
    assert "displaced by restore" in page.text


def test_detail_page_history_is_localized(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    _generate("# v1 body")
    _generate("# v2 body")
    response = c.put(
        "/api/preferences/workspace",
        json={
            "workspace_name": "localplaud",
            "theme": "light",
            "density": "comfortable",
            "timezone": "Asia/Taipei",
            "hour_cycle": "24",
            "locale": "zh-Hant-TW",
            "auto_process_new_recordings": False,
        },
    )
    assert response.status_code == 200
    page = c.get("/file/r1")
    assert "版本紀錄" in page.text
    assert "因重新產生而被取代" in page.text
    assert "不會排入佇列或重新產生" in page.text
    # The notes-tab restore copy states the mind-map consequence, localized.
    assert "依據這些筆記建立的心智圖將標示為已過期" in page.text


# --- Review-finding regressions ---------------------------------------------


def test_restore_marks_dependent_mind_map_out_of_date(monkeypatch, tmp_path):
    """Restoring a note version changes the mind map's source content, so the
    saved map is marked out of date (stale) — never presented, exported, or
    reused as current — while the artifact row itself is preserved and no
    processing is queued."""
    c = _client(monkeypatch, tmp_path)
    _seed(with_index=True)
    from localplaud.db.models import Chunk, StageName, StageRun, StageStatus
    from localplaud.db.session import session_scope
    from localplaud.worker.pipeline import _has_summary

    _generate("# v1 body")
    _generate("# v2 body")
    _generate("# Sync topics\n- agenda\n  - budget\n- decisions", template="mind_map",
              snapshot={"source_template_key": "default", "source_template_version": 1})
    with session_scope() as s:
        # The pipeline's completed bookkeeping for the generated map.
        s.add(StageRun(file_id="r1", stage=StageName.mind_map,
                       status=StageStatus.completed, attempts=2, detail={}))
        note_id = _live(s).id
        map_content = _live(s, "mind_map").content_md

    assert 'id="mindmap-src"' in c.get("/file/r1").text
    assert c.get("/file/r1/export/mind-map.png").status_code == 200
    assert _has_summary("r1", "mind_map") is True

    restored = c.post(f"/file/r1/summaries/{note_id}/versions/1/restore",
                      data={"tab": "notes"}, follow_redirects=False)
    assert restored.status_code == 303

    with session_scope() as s:
        runs = {run.stage: run for run in
                s.scalars(select(StageRun).where(StageRun.file_id == "r1"))}
        assert len(runs) == 3  # index + summarize + mind_map, nothing phantom
        map_run = runs[StageName.mind_map]
        assert map_run.status == StageStatus.pending
        assert map_run.detail["stale"] is True
        assert map_run.detail["reason"] == "note version restored"
        assert map_run.detail["stale_generation"]
        assert map_run.error is None and map_run.completed_at is None
        assert map_run.attempts == 2  # real attempt history preserved
        # Unrelated stages and artifacts stay exactly as they were.
        assert runs[StageName.summarize].status == StageStatus.completed
        assert runs[StageName.index].status == StageStatus.completed
        assert not (runs[StageName.summarize].detail or {}).get("stale")
        assert not (runs[StageName.index].detail or {}).get("stale")
        assert s.query(Chunk).filter_by(file_id="r1").count() == 1
        # The map artifact row is preserved, only presented as out of date.
        assert _live(s, "mind_map").content_md == map_content
        assert _live(s).content_md == "# v1 body"

    # Reuse is blocked: the next notes run rebuilds the map from fresh input.
    assert _has_summary("r1", "mind_map") is False
    # Exporting the stale artifact as current would be untruthful.
    denied = c.get("/file/r1/export/mind-map.png")
    assert denied.status_code == 409
    assert "out of date" in denied.json()["detail"]
    # The workspace says so instead of showing the outdated tree — and since
    # the inputs (restored notes + local transcript) are current, it offers
    # the mind-map-only rebuild instead of a full notes regeneration.
    page = c.get("/file/r1")
    assert "Mind map is out of date." in page.text
    assert 'id="rebuild-mindmap"' in page.text
    assert "Rebuild it from the current notes" in page.text
    assert 'id="mindmap-src"' not in page.text
    assert "Sync topics" not in page.text


def test_mind_map_and_unrelated_template_restores_stale_nothing(monkeypatch, tmp_path):
    """Template isolation for the staleness marking: restoring the mind map
    itself or a template the map was not built from leaves everything
    untouched; only a map without recorded lineage is marked conservatively."""
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import StageName, StageRun
    from localplaud.db.session import session_scope

    _generate("# default v1")
    _generate("# default v2")
    _generate("# meeting v1", template="meeting")
    _generate("# meeting v2", template="meeting")
    _generate("# map v1", template="mind_map",
              snapshot={"source_template_key": "default", "source_template_version": 1})
    _generate("# map v2", template="mind_map",
              snapshot={"source_template_key": "default", "source_template_version": 2})
    with session_scope() as s:
        default_id = _live(s).id
        meeting_id = _live(s, "meeting").id
        map_id = _live(s, "mind_map").id

    def map_runs():
        with session_scope() as s:
            return list(s.scalars(select(StageRun).where(
                StageRun.file_id == "r1", StageRun.stage == StageName.mind_map)))

    # Restoring the mind map itself has no downstream dependent.
    assert c.post(f"/file/r1/summaries/{map_id}/versions/1/restore",
                  data={"tab": "mindmap"}, follow_redirects=False).status_code == 303
    assert map_runs() == []
    with session_scope() as s:
        assert _live(s, "mind_map").content_md == "# map v1"

    # The map records its source template; other templates leave it current.
    assert c.post(f"/file/r1/summaries/{meeting_id}/versions/1/restore",
                  data={"tab": "notes"}, follow_redirects=False).status_code == 303
    assert map_runs() == []
    page = c.get("/file/r1")
    assert "Mind map is out of date." not in page.text
    assert 'id="mindmap-src"' in page.text

    # A legacy map without recorded lineage cannot be proven current, so any
    # note restore marks it rather than silently presenting it as current.
    with session_scope() as s:
        _live(s, "mind_map").template_snapshot = None
    assert c.post(f"/file/r1/summaries/{default_id}/versions/1/restore",
                  data={"tab": "notes"}, follow_redirects=False).status_code == 303
    runs = map_runs()
    assert len(runs) == 1
    assert runs[0].detail["stale"] is True
    assert runs[0].detail["reason"] == "note version restored"
    assert runs[0].detail["stale_generation"]
    assert runs[0].attempts == 0  # created only to carry the marker


def test_editable_copy_and_ask_note_provenance_survive_restore(monkeypatch, tmp_path):
    """UserNote provenance is immutable across restores. An editable copy pins
    the exact version it was copied from (the live slot id alone cannot,
    because restore rewrites the slot in place); an Ask saved answer keeps its
    message link and never gains a summary linkage."""
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import AskMessage, AskThread, UserNote
    from localplaud.db.session import session_scope
    from localplaud.note_history import fingerprint_digest

    _generate("# v1 body")
    _generate("# v2 body")
    with session_scope() as s:
        live = _live(s)
        summary_id = live.id
        v2_digest = fingerprint_digest(live)
        v2_created = live.created_at

    copy = c.post(f"/api/files/r1/summaries/{summary_id}/editable-copy")
    assert copy.status_code == 201
    snapshot = copy.json()["source_summary_snapshot"]
    assert snapshot == {
        "template": "default",
        "template_version": 1,
        "llm_provider": "fake-llm",
        "model": "m-1",
        "created_at": v2_created.isoformat(),
        "content_fingerprint": v2_digest,
    }

    with session_scope() as s:
        s.add(AskThread(id="thread-1", file_id="r1", title="Weekly sync Q&A"))
        s.add(AskMessage(thread_id="thread-1", role="user", content="What was decided?"))
        s.add(AskMessage(thread_id="thread-1", role="assistant", content="grounded answer",
                         sources=[{"file_id": "r1", "filename": "Weekly Sync",
                                   "start": 1.0, "end": 2.0, "speaker": "SPEAKER_00",
                                   "text": "hello team"}]))
        s.flush()
        message_id = s.scalar(
            select(AskMessage.id).where(AskMessage.role == "assistant"))
    saved = c.post(f"/api/ask/messages/{message_id}/save-note", json={})
    assert saved.status_code == 201
    ask_note = saved.json()
    assert ask_note["source_type"] == "ask"
    assert ask_note["ask_message_id"] == message_id
    assert ask_note["source_summary_id"] is None
    assert ask_note["source_summary_snapshot"] is None

    assert c.post(f"/file/r1/summaries/{summary_id}/versions/1/restore",
                  data={"tab": "notes"}, follow_redirects=False).status_code == 303

    with session_scope() as s:
        live = _live(s)
        assert fingerprint_digest(live) != v2_digest  # the slot now holds v1
        note = s.scalars(select(UserNote).where(
            UserNote.source_summary_id == summary_id)).one()
        assert note.content_md == "# v2 body"  # copy content never touched
        assert note.source_summary_snapshot == snapshot  # provenance immutable
        # The pinned fingerprint still identifies the archived v2 exactly.
        v2_revision = next(r for r in _revisions(s) if r.content_md == "# v2 body")
        assert fingerprint_digest(v2_revision) == snapshot["content_fingerprint"]
        ask_row = s.scalars(select(UserNote).where(
            UserNote.ask_message_id == message_id)).one()
        assert ask_row.content_md == "grounded answer"
        assert ask_row.source_summary_id is None
        assert ask_row.source_summary_snapshot is None

    # Re-copying after the restore returns the existing copy, provenance intact.
    again = c.post(f"/api/files/r1/summaries/{summary_id}/editable-copy")
    assert again.json()["content_md"] == "# v2 body"
    assert again.json()["source_summary_snapshot"] == snapshot
    listed = c.get("/api/notes?file_id=r1").json()["notes"]
    assert {note["source_summary_snapshot"] is None for note in listed} == {True, False}


def test_restore_with_null_legacy_created_at_falls_back(monkeypatch, tmp_path):
    """Legacy pre-archival revisions may carry no creation time; restoring one
    must not write NULL into the non-null live created_at (IntegrityError/500).
    The archival time, then the current live time, are the truthful bounds."""
    c = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.session import get_engine, session_scope

    _generate("# v1 body")
    _generate("# v2 body")
    _generate("# v3 body")
    with session_scope() as s:
        summary_id = _live(s).id
        live_created = _live(s).created_at

    # Deployed pre-m8 libraries keep created_at nullable (the legacy DDL in
    # the migration test above); recreate that layout so the NULLs are real.
    engine = get_engine()
    with engine.begin() as conn:
        create_sql = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='summary_revisions'"
        ).scalar_one()
        assert "created_at DATETIME NOT NULL" in create_sql
        conn.exec_driver_sql(
            "ALTER TABLE summary_revisions RENAME TO summary_revisions_strict")
        conn.exec_driver_sql(create_sql.replace(
            "created_at DATETIME NOT NULL", "created_at DATETIME"))
        conn.exec_driver_sql(
            "INSERT INTO summary_revisions SELECT * FROM summary_revisions_strict")
        conn.exec_driver_sql("DROP TABLE summary_revisions_strict")
        conn.exec_driver_sql(
            "UPDATE summary_revisions SET created_at = NULL, archived_at = NULL "
            "WHERE revision = 1")
        conn.exec_driver_sql(
            "UPDATE summary_revisions SET created_at = NULL WHERE revision = 2")
    with session_scope() as s:
        revisions = _revisions(s)
        assert revisions[0].created_at is None and revisions[0].archived_at is None
        rev2_archived = revisions[1].archived_at
        assert revisions[1].created_at is None and rev2_archived is not None

    restored = c.post(f"/file/r1/summaries/{summary_id}/versions/1/restore",
                      data={"tab": "notes"}, follow_redirects=False)
    assert restored.status_code == 303  # no IntegrityError 500
    with session_scope() as s:
        live = _live(s)
        assert live.content_md == "# v1 body"
        # No truthful time survives for this version: keep the last known one.
        assert live.created_at == live_created

    restored = c.post(f"/file/r1/summaries/{summary_id}/versions/2/restore",
                      data={"tab": "notes"}, follow_redirects=False)
    assert restored.status_code == 303
    with session_scope() as s:
        live = _live(s)
        assert live.content_md == "# v2 body"
        # The archival time is the closest truthful bound that still exists.
        assert live.created_at == rev2_archived


def test_summary_history_migration_emits_cross_dialect_ddl():
    """PostgreSQL is a documented-supported deployment; the history columns
    must be addable there too, with types rendered by the target dialect. The
    executable SQLite path stays covered by the idempotency test above."""
    from sqlalchemy.dialects import postgresql, sqlite

    from localplaud.db.migrations import (
        _SUMMARY_HISTORY_COLUMNS,
        summary_history_migration_statements,
    )

    legacy = {
        "summary_revisions": {
            "id", "file_id", "template", "revision", "title", "content_md",
            "llm_provider", "model", "source", "template_version",
            "transcript_revision", "profile_snapshot", "created_at",
        },
        "summaries": {"id", "file_id", "template", "content_md", "source",
                      "created_at"},
    }
    statements = dict(summary_history_migration_statements(legacy, postgresql.dialect()))
    assert statements["summaries.restored_from_revision"] == (
        "ALTER TABLE summaries ADD COLUMN restored_from_revision INTEGER")
    assert statements["summary_revisions.archived_at"] == (
        "ALTER TABLE summary_revisions ADD COLUMN archived_at "
        "TIMESTAMP WITH TIME ZONE")
    assert statements["summary_revisions.archive_reason"].endswith(
        "archive_reason VARCHAR(32)")
    assert statements["summary_revisions.input_transcript_source"].endswith(
        "input_transcript_source VARCHAR(16)")
    assert statements["summary_revisions.template_snapshot"].endswith(
        "template_snapshot JSON")
    assert statements["summary_revisions.input_transcript_id"].endswith(
        "input_transcript_id INTEGER")

    sqlite_statements = dict(summary_history_migration_statements(legacy, sqlite.dialect()))
    assert set(sqlite_statements) == set(statements)
    assert sqlite_statements["summary_revisions.archived_at"].endswith(
        "archived_at DATETIME")

    # Fresh databases build the whole layout through create_all.
    assert summary_history_migration_statements({}, postgresql.dialect()) == []
    # A fully current library needs nothing.
    current = {
        table: columns | {c for t, c, _ in _SUMMARY_HISTORY_COLUMNS if t == table}
        for table, columns in legacy.items()
    }
    assert summary_history_migration_statements(current, postgresql.dialect()) == []


def test_editable_note_provenance_migration_backfills_provable_copies(
    monkeypatch, tmp_path
):
    """Existing editable copies gain the provenance column on upgrade, and the
    backfill records only what can be proven: an unedited copy matches the
    live summary or one of its archived versions. Edited copies stay NULL —
    unknown is reported as unknown — and note content is never touched."""
    _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.migrations import migrate_editable_note_provenance_schema
    from localplaud.db.models import UserNote
    from localplaud.db.session import get_engine, session_scope
    from localplaud.note_history import fingerprint_digest

    _generate("# v1 body")
    _generate("# v2 body")
    _generate("# meeting body", template="meeting")
    _generate("# actions body", template="actions")
    with session_scope() as s:
        default_id = _live(s).id
        meeting = _live(s, "meeting")
        meeting_digest = fingerprint_digest(meeting)
        rev1 = _revisions(s)[0]
        rev1_digest = fingerprint_digest(rev1)
        rev1_created = rev1.created_at
        # Pre-upgrade copies: linked to their slot, no snapshot recorded yet.
        s.add(UserNote(file_id="r1", title="live copy", content_md="# meeting body",
                       source_type="generated_summary", source_summary_id=meeting.id))
        s.add(UserNote(file_id="r1", title="archived copy", content_md="# v1 body",
                       source_type="generated_summary", source_summary_id=default_id))
        s.add(UserNote(file_id="r1", title="edited copy",
                       content_md="# rewritten by hand",
                       source_type="generated_summary",
                       source_summary_id=_live(s, "actions").id))

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE user_notes DROP COLUMN source_summary_snapshot"))

    assert migrate_editable_note_provenance_schema(engine) == [
        "user_notes.source_summary_snapshot"
    ]
    assert migrate_editable_note_provenance_schema(engine) == []  # idempotent

    with session_scope() as s:
        notes = {note.title: note for note in s.scalars(select(UserNote))}
        live_copy = notes["live copy"]
        assert live_copy.content_md == "# meeting body"
        assert live_copy.source_summary_snapshot["template"] == "meeting"
        assert live_copy.source_summary_snapshot["content_fingerprint"] == meeting_digest
        archived_copy = notes["archived copy"]
        assert archived_copy.content_md == "# v1 body"
        assert archived_copy.source_summary_snapshot["template"] == "default"
        assert archived_copy.source_summary_snapshot["content_fingerprint"] == rev1_digest
        assert archived_copy.source_summary_snapshot["created_at"] == (
            rev1_created.isoformat())
        assert notes["edited copy"].content_md == "# rewritten by hand"
        assert notes["edited copy"].source_summary_snapshot is None

    # Databases the editable-copy feature never reached have nothing to pin.
    bare = create_engine(f"sqlite:///{tmp_path / 'bare.db'}")
    assert migrate_editable_note_provenance_schema(bare) == []
    with bare.begin() as connection:
        connection.execute(text(
            "CREATE TABLE user_notes (id INTEGER PRIMARY KEY, content_md TEXT)"))
    assert migrate_editable_note_provenance_schema(bare) == []


def test_history_loading_is_query_bounded(monkeypatch, tmp_path):
    """The detail page and history API bound archived-content loading in the
    query itself (window rank / LIMIT / COUNT) instead of materializing every
    archived body and slicing in Python."""
    c = _client(monkeypatch, tmp_path)
    _seed()
    from sqlalchemy import event

    from localplaud.db.session import get_engine, session_scope

    for index in range(25):
        _generate(f"# body {index}")
    with session_scope() as s:
        summary_id = _live(s).id

    captured: list[str] = []
    engine = get_engine()

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        captured.append(statement)

    def bounded(statements: list[str]) -> None:
        selects = [item.lower() for item in statements
                   if "summary_revisions" in item.lower()
                   and item.lstrip().lower().startswith("select")]
        assert selects, "expected summary_revisions reads"
        for statement in selects:
            assert ("limit" in statement or "count" in statement
                    or "row_number" in statement), statement

    try:
        history = c.get(f"/api/files/r1/summaries/{summary_id}/history?limit=5")
        body = history.json()
        assert body["version_count"] == 24
        assert [v["revision"] for v in body["versions"]] == [24, 23, 22, 21, 20]
        bounded(captured)

        captured.clear()
        page = c.get("/file/r1")
        assert page.status_code == 200
        # Exactly the preview window is rendered; the total stays truthful.
        assert page.text.count("data-version-row>") == 20
        assert "Version history · 24" in page.text  # truthful total, not 20
        assert "Older versions are kept and available through the API." in page.text
        bounded(captured)
        assert any("row_number" in item.lower() for item in captured)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
