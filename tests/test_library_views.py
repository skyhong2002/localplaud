"""Tests for library sorting, filtering, and trash/uncategorized views."""

from __future__ import annotations


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'lib.db'}")
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed():
    from localplaud.db.models import FileStatus, PlaudFile
    from localplaud.db.session import session_scope

    # Mixed names, durations, scenes, statuses, and one trashed row.
    rows = [
        # id, filename, status, duration_ms, start_time_ms, scene, is_trash
        ("a", "Alpha meeting", FileStatus.done, 300000, 1000, 1, False),
        ("b", "Bravo call", FileStatus.error, 900000, 2000, 1, False),
        ("c", "Charlie note", FileStatus.partial, 120000, 3000, 2, False),
        ("d", "Delta memo", FileStatus.processing, 600000, 4000, 2, False),
        ("t", "Trashed thing", FileStatus.done, 60000, 5000, 1, True),
    ]
    with session_scope() as s:
        for fid, name, st, dur, start, scene, trash in rows:
            s.add(
                PlaudFile(
                    id=fid,
                    filename=name,
                    status=st,
                    duration_ms=dur,
                    start_time_ms=start,
                    scene=scene,
                    is_trash=trash,
                )
            )


def _ids(client, query=""):
    return [f["id"] for f in client.get(f"/api/files{query}").json()["files"]]


def test_default_excludes_trash_and_sorts_recent_desc(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    ids = _ids(c)
    assert "t" not in ids  # trash excluded by default
    # newest start_time_ms first
    assert ids == ["d", "c", "b", "a"]


def test_sort_name_asc_and_desc(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    assert _ids(c, "?sort=name&dir=asc") == ["a", "b", "c", "d"]
    assert _ids(c, "?sort=name&dir=desc") == ["d", "c", "b", "a"]


def test_sort_duration(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    # durations: c=120k, a=300k, d=600k, b=900k
    assert _ids(c, "?sort=duration&dir=asc") == ["c", "a", "d", "b"]
    assert _ids(c, "?sort=duration&dir=desc") == ["b", "d", "a", "c"]


def test_state_filter(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    assert _ids(c, "?state=error") == ["b"]
    assert _ids(c, "?state=done") == ["a"]  # trashed 't' also done but excluded


def test_scene_filter(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    assert set(_ids(c, "?scene=2")) == {"c", "d"}
    assert set(_ids(c, "?scene=1")) == {"a", "b"}  # 't' scene 1 but trashed


def test_trash_view_inclusion(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    assert _ids(c, "?view=trash") == ["t"]


def test_query_combines_with_filter(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    # q matches Bravo (error) and would also match nothing else in scene 2
    assert _ids(c, "?q=bravo&state=error") == ["b"]
    assert _ids(c, "?q=note&scene=2") == ["c"]


def test_invalid_params_fall_back(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    # bogus sort/dir/state/scene/view must not 500 and behave like defaults
    r = c.get("/api/files?sort=bogus&dir=sideways&state=nope&scene=abc&view=weird")
    assert r.status_code == 200
    assert [f["id"] for f in r.json()["files"]] == ["d", "c", "b", "a"]


def test_index_page_renders_table_and_controls(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/")
    assert r.status_code == 200
    assert "rectable" in r.text  # sortable table present
    assert "Bravo call" in r.text
    assert "Trash" in r.text  # trash view link
    assert "Source 1" in r.text  # capture-source facet


def test_index_trash_view_shows_banner(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/?view=trash")
    assert r.status_code == 200
    assert "Trashed thing" in r.text
    assert "read-only recovery view" in r.text
    assert "never deletes" in r.text


def test_index_invalid_params_ok(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    _seed()
    r = c.get("/?sort=xyz&dir=nope&state=bad&scene=notint&view=??")
    assert r.status_code == 200
