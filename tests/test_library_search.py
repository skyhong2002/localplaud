"""Library search works locally and respects artifact provenance and filters."""

from __future__ import annotations


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'search.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed():
    from localplaud.db.models import (
        Folder,
        PlaudFile,
        Summary,
        Tag,
        Transcript,
        TranscriptRevision,
        UserNote,
    )
    from localplaud.db.session import session_scope

    with session_scope() as session:
        folder = Folder(name="Research")
        tag = Tag(name="Decision")
        local = PlaudFile(
            id="local-recording",
            filename="Original title",
            local_title="Launch meeting",
            origin="local",
            start_time_ms=1783582737000,
            folder=folder,
            tags=[tag],
        )
        cloud = PlaudFile(
            id="cloud-recording",
            filename="Plaud interview",
            origin="plaud",
            start_time_ms=1780990737000,
        )
        session.add_all([local, cloud])
        session.flush()
        raw = Transcript(
            file_id=local.id,
            provider="test",
            source="local",
            text="the launch was delayed",
            segments=[{"text": "the launch was delayed", "start": 12.5, "end": 16.0, "speaker": "Sky"}],
        )
        session.add(raw)
        session.flush()
        session.add(
            TranscriptRevision(
                file_id=local.id,
                base_transcript_id=raw.id,
                revision=1,
                source="local",
                text="the launch was approved",
                segments=[{"text": "the launch was approved", "start": 12.5, "end": 16.0, "speaker": "Sky"}],
            )
        )
        session.add(
            Transcript(
                file_id=cloud.id,
                provider="plaud",
                source="cloud",
                text="exclusive cloud phrase",
                segments=[{"text": "exclusive cloud phrase", "start": 4.0, "end": 6.0}],
            )
        )
        session.add(
            Summary(
                file_id=local.id,
                template="meeting",
                source="local",
                content_md="Decision owner is Morgan.",
            )
        )
        session.add(
            UserNote(file_id=local.id, title="Follow-up", content_md="Call Riley tomorrow.")
        )


def test_lexical_search_uses_corrected_canonical_transcript(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.library_search import lexical_search

    hit = lexical_search("approved")[0]
    assert hit["file_id"] == "local-recording"
    assert hit["kind"] == "transcript"
    assert hit["start"] == 12.5
    assert lexical_search("delayed") == []
    assert lexical_search("Morgan")[0]["kind"] == "note"
    assert lexical_search("Riley")[0]["kind"] == "note"
    assert lexical_search("Launch meeting")[0]["kind"] == "title"


def test_lexical_search_filters_and_cloud_provenance(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.config import Settings
    from localplaud.db.models import Folder, Tag
    from localplaud.db.session import session_scope
    from localplaud.library_search import lexical_search

    with session_scope() as session:
        folder_id = session.query(Folder).filter_by(name="Research").one().id
        tag_id = session.query(Tag).filter_by(name="Decision").one().id

    assert lexical_search("cloud phrase") == []
    migration = Settings(pipeline={"artifact_mode": "migration", "prefer_cloud_artifacts": True})
    assert lexical_search("cloud phrase", settings=migration)[0]["file_id"] == "cloud-recording"
    assert lexical_search("approved", folder_id=folder_id, tag_id=tag_id, origin="local")
    assert lexical_search("approved", origin="plaud") == []
    assert lexical_search("approved", date_from_ms=1784000000000) == []


def test_search_page_works_without_embeddings_and_links_timestamp(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()

    import localplaud.worker.qa as qa

    def unavailable(*args, **kwargs):
        raise RuntimeError("embedding provider unavailable")

    monkeypatch.setattr(qa, "retrieve", unavailable)
    response = client.get("/search", params={"q": "approved", "origin": "local"})
    assert response.status_code == 200
    assert "Launch meeting" in response.text
    assert "/file/local-recording?t=12.5" in response.text
    assert "Transcript" in response.text
    assert "embedding provider isn&#39;t available" not in response.text


def test_search_page_applies_filters_to_semantic_hits(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()

    import localplaud.worker.qa as qa

    monkeypatch.setattr(
        qa,
        "retrieve",
        lambda *args, **kwargs: [
            {"file_id": "cloud-recording", "filename": "Plaud interview", "text": "meaning", "start": 2.0, "end": 3.0, "speaker": None, "score": 0.8},
            {"file_id": "local-recording", "filename": "Launch meeting", "text": "meaning", "start": 2.0, "end": 3.0, "speaker": None, "score": 0.7},
        ],
    )
    response = client.get("/search", params={"q": "meaning", "origin": "local"})
    assert response.status_code == 200
    assert "Launch meeting" in response.text
    assert "Plaud interview" not in response.text
    assert "Semantic" in response.text
