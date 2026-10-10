"""Accounts see only their own workspace through every Web and API surface."""

import re

import pytest
from sqlalchemy import select

from localplaud.db.models import AccountUser, Folder, PlaudFile, Summary, Tag, Transcript
from localplaud.db.session import session_scope
from localplaud.db.tenancy import local_file_id, system_scope, workspace_scope
from localplaud.workspaces import ensure_user_workspace

from .test_accounts import client, create_user, login  # noqa: F401


def seed(workspace_id: int, file_id: str, word: str) -> None:
    with workspace_scope(workspace_id), session_scope() as db:
        recording = PlaudFile(
            id=file_id,
            filename=f"{word} meeting",
            status="done",
            start_time_ms=1_700_000_000_000,
            duration_ms=60_000,
        )
        recording.transcripts.append(
            Transcript(
                provider="test",
                text=f"{word} secret plan",
                segments=[{"start": 0, "end": 1, "text": f"{word} secret plan"}],
            )
        )
        recording.summaries.append(Summary(template="meeting", content_md=f"# {word} notes"))
        recording.tags.append(Tag(name=f"{word}-tag"))
        db.add(recording)
        db.add(Folder(name=f"{word} folder"))


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
    seed(1, "mine", "owner")
    seed(member_workspace, theirs, "member")
    return client, theirs


def test_listing_search_and_organization_are_private(two_workspaces):
    web, theirs = two_workspaces
    login(web, "member")
    files = web.get("/api/files").json()["files"]
    assert [row["id"] for row in files] == [theirs]
    organization = web.get("/api/organization").json()
    assert "owner folder" not in str(organization) and "owner-tag" not in str(organization)
    assert "owner" not in web.get("/api/search", params={"q": "secret"}).text
    login(web, "sky")
    assert [row["id"] for row in web.get("/api/files").json()["files"]] == ["mine"]
    assert "member" not in web.get("/api/search", params={"q": "secret"}).text


def test_every_recording_route_hides_another_workspace(two_workspaces):
    from localplaud.api.app import app

    web, theirs = two_workspaces
    login(web, "sky")
    leaked = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if "{file_id}" not in path:
            continue
        concrete = re.sub(r"\{file_id\}", theirs, path)
        concrete = re.sub(r"\{[^}]+\}", "1", concrete)
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            response = web.request(method, concrete, json={})
            body = response.text
            if response.status_code < 400 or "member secret" in body or "member notes" in body:
                leaked.append((method, path, response.status_code))
    assert leaked == []
    # Positive control: the same routes do serve the recording to its owner.
    login(web, "member")
    page = web.get(f"/file/{theirs}")
    assert page.status_code == 200 and "member meeting" in page.text
    assert web.get(f"/file/{theirs}/export/transcript.txt").status_code == 200
    with system_scope(), session_scope() as db:
        recording = db.get(PlaudFile, theirs)
        assert recording.filename == "member meeting" and not recording.is_trash


def test_bulk_and_export_requests_cannot_reach_another_workspace(two_workspaces):
    web, theirs = two_workspaces
    login(web, "sky")
    response = web.post(
        "/api/files/export", json={"file_ids": [theirs], "transcript_format": "txt"}
    )
    assert response.status_code == 404
    web.post("/api/files/bulk", json={"file_ids": [theirs], "action": "trash"})
    with system_scope(), session_scope() as db:
        assert db.get(PlaudFile, theirs).is_trash is False


def test_share_links_open_publicly_but_only_for_their_issuer(two_workspaces):
    web, theirs = two_workspaces
    login(web, "member")
    link = web.post(f"/api/files/{theirs}/share-link", json={}).json()
    token = link["url"].rsplit("/", 1)[1]
    login(web, "sky")
    assert web.post(f"/api/files/{theirs}/share-link", json={}).status_code == 404
    web.cookies.clear()
    page = web.get(f"/share/{token}")
    assert page.status_code == 200 and "member" in page.text
    assert web.get("/share/not-a-token").status_code == 404


def test_preferences_and_vocabulary_are_per_workspace(two_workspaces):
    web, _ = two_workspaces
    login(web, "member")
    prefs = web.get("/api/preferences/workspace").json()
    assert prefs["workspace_name"] == "member"
    web.put("/api/preferences/workspace", json={**prefs, "workspace_name": "Mine only"})
    web.post("/api/vocabulary", json={"source_text": "plaud", "replacement_text": "Plaud"})
    login(web, "sky")
    assert web.get("/api/preferences/workspace").json()["workspace_name"] != "Mine only"
    assert web.get("/api/vocabulary").json() in ([], {"terms": []}) or "plaud" not in str(
        web.get("/api/vocabulary").json()
    )
    created = web.post(
        "/api/vocabulary", json={"source_text": "plaud", "replacement_text": "Plaud"}
    )
    assert created.status_code == 201


def test_rows_written_by_requests_belong_to_the_signed_in_workspace(two_workspaces):
    web, _ = two_workspaces
    login(web, "member")
    assert web.post("/api/folders", json={"name": "Member only"}).status_code in {200, 201}
    with system_scope(), session_scope() as db:
        member_workspace = db.scalar(select(PlaudFile.workspace_id).where(PlaudFile.id != "mine"))
        folder = db.scalar(select(Folder).where(Folder.name == "Member only"))
        assert folder.workspace_id == member_workspace


def test_members_cannot_use_host_secrets_or_private_networks(two_workspaces, monkeypatch):
    from localplaud import integrations

    web, _ = two_workspaces
    monkeypatch.setenv("HOST_ONLY_SECRET", "sk-host-secret")
    login(web, "member")
    for body in (
        {"name": "x", "url": "https://hooks.example.com/x", "secret_ref": "env:HOST_ONLY_SECRET"},
        {"name": "x", "url": "http://127.0.0.1:9/x", "allow_private_network": True},
    ):
        response = web.post("/api/integrations/webhooks", json=body)
        assert response.status_code == 422 and "owner" in response.text
    email = {
        "name": "mail",
        "smtp_host": "smtp.example.com",
        "username": "me",
        "password_ref": "env:HOST_ONLY_SECRET",
        "from_address": "me@example.com",
        "to_addresses": ["me@example.com"],
    }
    assert web.post("/api/integrations/emails", json=email).status_code == 422
    # Delivery refuses even a row that somehow carries a host secret reference.
    with workspace_scope(2):
        with pytest.raises(ValueError, match="owner"):
            integrations._secret_value("env:HOST_ONLY_SECRET")
    with workspace_scope(1):
        assert integrations._secret_value("env:HOST_ONLY_SECRET") == "sk-host-secret"


def test_external_rule_ids_are_independent_per_workspace(two_workspaces):
    web, _ = two_workspaces
    body = {
        "owner_key": "notion-sync",
        "owner_label": "Notion Sync",
        "external_id": "rule-42",
        "name": "External filing",
        "trigger": {"origin": "plaud"},
        "actions": {"note_template_key": "plaud-autopilot"},
    }
    login(web, "sky")
    assert web.put("/api/automations/external-rules", json=body).json()["created"] is True
    login(web, "member")
    assert web.put("/api/automations/external-rules", json=body).json()["created"] is True


def test_workspace_name_always_matches_the_account(two_workspaces):
    web, _ = two_workspaces
    for username in ("sky", "member"):
        login(web, username)
        prefs = web.get("/api/preferences/workspace").json()
        assert prefs["workspace_name"] == username and prefs["workspace_name_locked"]
        saved = web.put(
            "/api/preferences/workspace", json={**prefs, "workspace_name": "Renamed"}
        ).json()
        assert saved["workspace_name"] == username
        assert web.get("/api/preferences/workspace").json()["workspace_name"] == username
