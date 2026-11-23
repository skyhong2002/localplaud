"""Library title search follows the displayed local title and workspace scope."""

from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest

from localplaud.db.models import FileStatus, Folder, PlaudFile, Tag, Workspace
from localplaud.db.session import session_scope
from localplaud.db.tenancy import system_scope, workspace_scope


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path / 'titles.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _ids(client, **params):
    response = client.get("/api/files", params=params)
    assert response.status_code == 200
    return [row["id"] for row in response.json()["files"]]


def _picker_ids(client, q):
    response = client.get("/api/files/picker", params={"q": q})
    assert response.status_code == 200
    return [row["id"] for row in response.json()["recordings"]]


def test_generated_title_only_matches_library_picker_and_rendered_counts(client):
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="generated",
                filename="REC_20261123.wav",
                generated_title="合成專案：季度規劃",
                generated_title_provider="test",
                generated_title_model="synthetic",
                status=FileStatus.done,
                origin="local",
            )
        )

    query = {"q": "季度規劃"}
    assert _ids(client, **query) == ["generated"]
    assert _picker_ids(client, query["q"]) == ["generated"]
    page = client.get("/", params=query)
    assert page.status_code == 200
    assert page.context["pagination"]["total"] == 1
    assert [row["id"] for row in page.context["files"]] == ["generated"]
    assert "合成專案：季度規劃" in page.text

    detail = client.get("/file/generated", params={"return_to": "/?" + urlencode(query)})
    assert detail.status_code == 200
    assert detail.context["filelist_context"]["total"] == 1
    assert not detail.context["files"][0].get("pinned")

    empty = client.get("/", params={"q": "unmatched synthetic subject"})
    assert empty.status_code == 200
    assert empty.context["pagination"]["total"] == 0
    assert empty.context["files"] == []


def test_manual_rename_wins_and_original_filename_remains_searchable(client):
    with session_scope() as session:
        session.add(
            PlaudFile(
                id="renamed",
                filename="original-source.wav",
                generated_title="Generated subject",
                local_title="Renamed workshop",
                status=FileStatus.done,
            )
        )

    for query in ("renamed WORKSHOP", "original-source"):
        assert _ids(client, q=query) == ["renamed"]
        assert _picker_ids(client, query) == ["renamed"]
    assert _ids(client, q="Generated subject") == []
    assert _picker_ids(client, "Generated subject") == []


def test_empty_titles_follow_display_title_fallbacks(client):
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id="empty-override",
                    filename="source.wav",
                    local_title="",
                    generated_title="Visible generated fallback",
                ),
                PlaudFile(
                    id="filename",
                    filename="Visible filename fallback",
                    local_title="",
                    generated_title="",
                ),
                PlaudFile(
                    id="idfallback12-hidden-suffix", filename="", local_title="", generated_title=""
                ),
            ]
        )

    for query, expected in (
        ("Visible generated", "empty-override"),
        ("Visible filename", "filename"),
        ("idfallback12", "idfallback12-hidden-suffix"),
    ):
        assert _ids(client, q=query) == [expected]
        assert _picker_ids(client, query) == [expected]
    assert _ids(client, q="hidden-suffix") == []


@pytest.mark.parametrize("literal", ["%", "_", "\\", "\\%_"])
def test_generated_title_search_escapes_literal_like_characters(client, literal):
    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(
                    id="literal",
                    filename="capture.wav",
                    generated_title=f"Synthetic {literal} marker",
                ),
                PlaudFile(
                    id="decoy", filename="ordinary.wav", generated_title="Synthetic ordinary marker"
                ),
            ]
        )

    assert _ids(client, q=literal) == ["literal"]
    assert _picker_ids(client, literal) == ["literal"]


def test_generated_title_search_composes_with_filters_counts_and_calendar(client):
    start_ms = int(datetime(2026, 11, 22, 16, tzinfo=UTC).timestamp() * 1000)
    with session_scope() as session:
        folder, other_folder = Folder(name="Selected"), Folder(name="Other")
        tag, other_tag = Tag(name="Selected"), Tag(name="Other")
        session.add_all([folder, other_folder, tag, other_tag])
        session.flush()
        filters = {"folder": folder.id, "tag": tag.id, "origin": "local", "scene": 1}
        for file_id, overrides in (
            ("match", {}),
            ("wrong-folder", {"folder": other_folder}),
            ("wrong-tag", {"tags": [other_tag]}),
            ("wrong-origin", {"origin": "plaud"}),
            ("wrong-source", {"scene": 2}),
            ("wrong-date", {"start_time_ms": start_ms - 1}),
            ("wrong-duration", {"duration_ms": 600_000}),
            ("wrong-state", {"status": FileStatus.error}),
            ("trash", {"is_trash": True}),
        ):
            values = dict(
                filename=f"{file_id}.wav",
                generated_title="Synthetic scope subject",
                folder=folder,
                tags=[tag],
                origin="local",
                scene=1,
                start_time_ms=start_ms,
                duration_ms=60_000,
                status=FileStatus.done,
            )
            session.add(PlaudFile(id=file_id, **(values | overrides)))

    preferences = client.get("/api/preferences/workspace").json()
    assert (
        client.put(
            "/api/preferences/workspace",
            json=preferences
            | {
                "timezone": "Asia/Taipei",
            },
        ).status_code
        == 200
    )
    query = filters | {
        "q": "scope subject",
        "date_from": "2026-11-23",
        "date_to": "2026-11-23",
        "min_duration_minutes": 1,
        "max_duration_minutes": 1,
        "state": "done",
    }
    assert _ids(client, **query) == ["match"]
    page = client.get("/", params=query)
    assert page.status_code == 200
    assert page.context["pagination"]["total"] == 1
    assert [row["id"] for row in page.context["files"]] == ["match"]

    calendar = client.get("/calendar", params={"month": "2026-11"})
    assert calendar.status_code == 200
    day = next(
        day
        for week in calendar.context["cal"]["weeks"]
        for day in week
        if day["date"] == "2026-11-23"
    )
    assert day["count"] == len(_ids(client, date_from="2026-11-23", date_to="2026-11-23"))
    linked = client.get(day["href"], params=query)
    assert linked.status_code == 200
    assert linked.context["pagination"]["total"] == 1


def test_generated_title_matches_and_counts_stay_in_active_workspace(client):
    with system_scope(), session_scope() as session:
        session.add(Workspace(id=2, name="Other synthetic workspace"))
    for workspace_id, file_id in ((1, "mine"), (2, "theirs")):
        with workspace_scope(workspace_id), session_scope() as session:
            session.add(
                PlaudFile(
                    id=file_id, filename="capture.wav", generated_title="Shared synthetic subject"
                )
            )

    for workspace_id, file_id in ((1, "mine"), (2, "theirs")):
        with workspace_scope(workspace_id):
            assert _ids(client, q="synthetic subject") == [file_id]
            assert _picker_ids(client, "synthetic subject") == [file_id]
            page = client.get("/", params={"q": "synthetic subject"})
            assert page.status_code == 200
            assert page.context["pagination"]["total"] == 1
            assert [row["id"] for row in page.context["files"]] == [file_id]
