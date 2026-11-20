"""Recently used, manually assigned speaker names offered by the naming sheet."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from localplaud.db.models import AccountUser, PlaudFile, Speaker
from localplaud.db.session import session_scope
from localplaud.db.tenancy import local_file_id, workspace_scope
from localplaud.workspaces import ensure_user_workspace

from .test_accounts import client, create_user, login  # noqa: F401
from .test_outline_ui import run_node, workspace_functions  # noqa: F401
from .test_speakers import _client

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _recording(file_id: str, names: list[tuple[str, str | None]], *, minutes: int = 0,
               trash: bool = False) -> None:
    with session_scope() as s:
        s.add(PlaudFile(id=file_id, filename=file_id, status="done", is_trash=trash,
                        duration_ms=60_000, start_time_ms=1_700_000_000_000))
        s.flush()
        for index, (key, name) in enumerate(names):
            stamp = BASE + timedelta(minutes=minutes, seconds=index)
            s.add(Speaker(file_id=file_id, key=key, display_name=name,
                          created_at=stamp, updated_at=stamp))


def test_recent_names_are_manual_distinct_newest_first_and_bounded(monkeypatch, tmp_path):
    web = _client(monkeypatch, tmp_path)
    _recording("old", [("SPEAKER_00", "  Alice   Chen "), ("SPEAKER_01", None)], minutes=1)
    _recording("mid", [("SPEAKER_00", "alice chen"), ("SPEAKER_01", "王小明")], minutes=2)
    _recording("new", [("SPEAKER_00", "Bob"), ("SPEAKER_01", "")], minutes=3)
    _recording("gone", [("SPEAKER_00", "Trashed Person")], minutes=9, trash=True)

    body = web.get("/api/speaker-names/recent").json()
    assert [item["name"] for item in body["names"]] == ["Bob", "王小明", "alice chen"]
    assert body["limit"] == 8

    limited = web.get("/api/speaker-names/recent", params={"limit": 2}).json()
    assert [item["name"] for item in limited["names"]] == ["Bob", "王小明"]
    # Out-of-range limits are clamped rather than unbounded.
    assert web.get("/api/speaker-names/recent", params={"limit": 999}).json()["limit"] == 20
    assert web.get("/api/speaker-names/recent", params={"limit": 0}).json()["limit"] == 8

    excluded = web.get("/api/speaker-names/recent", params={"exclude_file_id": "new"}).json()
    assert [item["name"] for item in excluded["names"]] == ["王小明", "alice chen"]


def test_recent_names_empty_state(monkeypatch, tmp_path):
    web = _client(monkeypatch, tmp_path)
    _recording("r1", [("SPEAKER_00", None)])
    assert web.get("/api/speaker-names/recent").json()["names"] == []


def test_recent_names_skip_unreviewed_automatic_voice_matches(monkeypatch, tmp_path):
    from localplaud.db.session import get_engine
    from localplaud.voice_identity import VoiceAssignment, create_schema

    web = _client(monkeypatch, tmp_path)
    create_schema(get_engine())
    _recording("r1", [("SPEAKER_00", "Auto Guess"), ("SPEAKER_01", "Edited Later")], minutes=1)
    _recording("r2", [("SPEAKER_00", "Typed By Hand")], minutes=2)
    with session_scope() as s:
        speakers = {row.display_name: row for row in s.query(Speaker)}
        s.add(VoiceAssignment(speaker_id=speakers["Auto Guess"].id, file_id="r1",
                              speaker_key="SPEAKER_00", applied_name="Auto Guess",
                              status="applied", evidence={}))
        # The matcher applied a name, but a person has since changed it.
        s.add(VoiceAssignment(speaker_id=speakers["Edited Later"].id, file_id="r1",
                              speaker_key="SPEAKER_01", applied_name="Machine Name",
                              status="applied", evidence={}))
    names = [item["name"] for item in web.get("/api/speaker-names/recent").json()["names"]]
    assert names == ["Typed By Hand", "Edited Later"]


def test_renaming_a_speaker_makes_it_the_most_recent(monkeypatch, tmp_path):
    from localplaud.db.models import Transcript

    from .test_speakers import SEGMENTS, _mute_reindex

    web = _client(monkeypatch, tmp_path)
    _mute_reindex(monkeypatch)
    _recording("other", [("SPEAKER_00", "Earlier Name")], minutes=1)
    with session_scope() as s:
        s.add(PlaudFile(id="r1", filename="r1", status="done", duration_ms=60_000,
                        start_time_ms=1_700_000_000_000))
        s.add(Transcript(file_id="r1", provider="test", source="local", has_speakers=True,
                         text="x", segments=SEGMENTS))
    response = web.post("/file/r1/speakers", data={"key": "SPEAKER_01", "name": "Fresh Name"},
                        headers={"accept": "application/json"})
    assert response.status_code == 200
    names = [item["name"] for item in web.get("/api/speaker-names/recent").json()["names"]]
    assert names[0] == "Fresh Name" and "Earlier Name" in names


@pytest.fixture
def two_workspaces(client):  # noqa: F811
    from localplaud.db.session import init_db

    init_db()
    owner = create_user("sky", role="owner")
    member = create_user("member")
    with session_scope() as db:
        assert ensure_user_workspace(db, db.get(AccountUser, owner)) == 1
        member_workspace = ensure_user_workspace(db, db.get(AccountUser, member))
    theirs = local_file_id(member_workspace, "shared-plaud-id")
    with workspace_scope(1):
        _recording("mine", [("SPEAKER_00", "Owner Colleague")])
    with workspace_scope(member_workspace):
        _recording(theirs, [("SPEAKER_00", "Member Private Contact")])
    return client, theirs


def test_recent_names_never_cross_workspaces(two_workspaces):
    web, theirs = two_workspaces
    login(web, "member")
    body = web.get("/api/speaker-names/recent").json()
    assert [item["name"] for item in body["names"]] == ["Member Private Contact"]
    login(web, "sky")
    body = web.get("/api/speaker-names/recent").json()
    assert [item["name"] for item in body["names"]] == ["Owner Colleague"]
    assert "Member Private Contact" not in web.get(
        "/api/speaker-names/recent", params={"exclude_file_id": theirs}
    ).text


def test_recent_name_choices_skip_current_speakers_and_duplicates(workspace_functions):  # noqa: F811
    run_node(workspace_functions("recentNameChoices"), r"""
const names = [{name: 'Bob'}, {name: '  alice '}, {name: 'BOB'}, {name: ''}, {name: 'Carol'},
               {name: 'Dan'}, {name: 'Eve'}];
assert.deepEqual(recentNameChoices(names, ['Alice', 'Speaker 1'], 3), ['Bob', 'Carol', 'Dan']);
assert.deepEqual(recentNameChoices([], ['Alice'], 8), []);
assert.deepEqual(recentNameChoices(null, [], 8), []);
""")
