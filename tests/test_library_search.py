"""Library search works locally and respects artifact provenance and filters."""

from __future__ import annotations

import pytest


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
        Speaker,
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
            segments=[{"text": "the launch was delayed", "start": 12.5, "end": 16.0, "speaker": "SPEAKER_00"}],
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
                segments=[{"text": "the launch was approved", "start": 12.5, "end": 16.0, "speaker": "SPEAKER_00"}],
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
        session.add_all(
            [
                Summary(
                    file_id=local.id,
                    template="meeting",
                    source="local",
                    content_md="Decision owner is Morgan.",
                ),
                Summary(
                    file_id=local.id,
                    template="mind_map",
                    source="local",
                    content_md="# Roadmap\n- Launch branch alpha",
                ),
                Speaker(file_id=local.id, key="SPEAKER_00", display_name="Sky"),
            ]
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
    assert hit["speaker"] == "Sky"
    assert hit["speaker_key"] == "SPEAKER_00"
    assert lexical_search("delayed") == []
    generated = lexical_search("Morgan")[0]
    assert generated["kind"] == "note"
    assert generated["target"] == "generated_note" and generated["artifact_id"]
    # User-owned saved notes report their own kind so results label them
    # distinctly from generated notes.
    saved = lexical_search("Riley")[0]
    assert saved["kind"] == "saved_note"
    assert saved["target"] == "saved_note" and saved["artifact_id"]
    mind_map = lexical_search("alpha")[0]
    assert mind_map["target"] == "mind_map" and mind_map["artifact_id"]
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
    # The playable timestamp is the transcript hit's label; no jargon chip.
    assert '<span class="search-kind">Transcript</span>' not in response.text
    assert "0:12" in response.text
    assert "embedding provider isn&#39;t available" not in response.text


def test_search_note_hits_link_to_exact_workspace_artifact(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    from localplaud.db.models import Summary, UserNote
    from localplaud.db.session import session_scope

    monkeypatch.setattr("localplaud.worker.qa.retrieve", lambda *args, **kwargs: [])
    with session_scope() as session:
        generated_id = session.query(Summary).filter_by(template="meeting").one().id
        saved_id = session.query(UserNote).one().id

    generated = client.get("/search", params={"q": "Morgan"})
    saved = client.get("/search", params={"q": "Riley"})
    mind_map = client.get("/search", params={"q": "alpha"})
    transcript = client.get("/search", params={"q": "approved"})
    assert f"/file/local-recording?tab=notes&amp;note=sum-{generated_id}" in generated.text
    assert f"/file/local-recording?tab=notes&amp;note_id={saved_id}" in saved.text
    assert "/file/local-recording?tab=mindmap" in mind_map.text
    assert "Sky" in transcript.text
    assert "SPEAKER_00" not in transcript.text


def test_search_page_applies_filters_to_semantic_hits(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()

    import localplaud.worker.qa as qa

    calls = []

    def fake_retrieve(*args, **kwargs):
        calls.append(kwargs)
        return [
            {"file_id": "cloud-recording", "filename": "Plaud interview", "text": "meaning", "start": 2.0, "end": 3.0, "speaker": None, "score": 0.8},
            {"file_id": "local-recording", "filename": "Launch meeting", "text": "meaning", "start": None, "end": None, "speaker": None, "score": 0.7},
        ]

    monkeypatch.setattr(qa, "retrieve", fake_retrieve)
    response = client.get("/search", params={"q": "meaning", "origin": "local"})
    assert response.status_code == 200
    assert "Launch meeting" in response.text
    assert "Plaud interview" not in response.text
    assert calls == [{"top_k": 30, "retrieval_scope": {"origin": "local"}}]
    # Semantic blends get a quiet Related label instead of provider jargon.
    assert ">Related<" in response.text
    assert ">Semantic<" not in response.text


def test_search_date_filter_uses_workspace_timezone_before_semantic_ranking(
    monkeypatch, tmp_path
):
    client = _client(monkeypatch, tmp_path)
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id="before",
                    filename="Boundary before",
                    start_time_ms=1_784_131_199_000,
                ),
                PlaudFile(
                    id="lower",
                    filename="Boundary lower",
                    start_time_ms=1_784_131_200_000,
                ),
                PlaudFile(
                    id="upper",
                    filename="Boundary upper",
                    start_time_ms=1_784_217_599_999,
                ),
                PlaudFile(
                    id="after",
                    filename="Boundary after",
                    start_time_ms=1_784_217_600_000,
                ),
            ]
        )

    scopes = []
    import localplaud.worker.qa as qa

    def unavailable(*args, **kwargs):
        scopes.append(kwargs["retrieval_scope"])
        raise RuntimeError("embedding provider unavailable")

    monkeypatch.setattr(qa, "retrieve", unavailable)
    response = client.get(
        "/search",
        params={
            "q": "Boundary",
            "date_from": "2026-07-16",
            "date_to": "2026-07-16",
        },
    )
    assert response.status_code == 200
    assert "Boundary lower" in response.text and "Boundary upper" in response.text
    assert "Boundary before" not in response.text and "Boundary after" not in response.text
    assert scopes == [
        {
            "scope_version": 2,
            "date_timezone": "Asia/Taipei",
            "date_from": "2026-07-16",
            "date_from_ms": 1_784_131_200_000,
            "date_to": "2026-07-16",
            "date_to_ms_exclusive": 1_784_217_600_000,
        }
    ]
    assert "Recorded date · Asia/Taipei" in response.text


def test_search_reversed_date_range_fails_closed(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed()
    import localplaud.worker.qa as qa

    calls = []
    monkeypatch.setattr(qa, "retrieve", lambda *args, **kwargs: calls.append(kwargs))
    response = client.get(
        "/search",
        params={"q": "approved", "date_from": "2026-07-02", "date_to": "2026-07-01"},
    )
    assert response.status_code == 200
    assert "Start date must not follow end date." in response.text
    assert "Launch meeting" not in response.text
    assert calls == []


@pytest.mark.parametrize("invalid_date", ["not-a-date", "9999-12-31"])
def test_search_invalid_date_fails_closed_before_semantic_retrieval(
    monkeypatch, tmp_path, invalid_date
):
    client = _client(monkeypatch, tmp_path)
    _seed()
    import localplaud.worker.qa as qa

    calls = []
    monkeypatch.setattr(qa, "retrieve", lambda *args, **kwargs: calls.append(kwargs))
    response = client.get(
        "/search",
        params={"q": "approved", "date_to": invalid_date},
    )
    assert response.status_code == 200
    assert "Enter a valid recorded date in the supported range." in response.text
    assert "Launch meeting" not in response.text
    assert calls == []
