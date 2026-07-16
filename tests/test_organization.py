"""Folder/tag metadata, migration, and organization API behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, inspect, text


def _client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import localplaud.db.session as db_session
    from localplaud.config import get_settings

    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{tmp_path/'org.db'}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    get_settings(reload=True)
    from localplaud.api.app import app
    from localplaud.db.session import init_db

    init_db()
    return TestClient(app)


def _seed_files():
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add_all(
            [
                PlaudFile(id="a", filename="Alpha", start_time_ms=1),
                PlaudFile(id="b", filename="Bravo", start_time_ms=2),
                PlaudFile(id="trash", filename="Trash", start_time_ms=3, is_trash=True),
            ]
        )


def test_additive_organization_migration_is_idempotent(tmp_path):
    from localplaud.db.migrations import migrate_organization_schema

    engine = create_engine(f"sqlite:///{tmp_path/'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO plaud_files (id) VALUES ('kept')"))

    first = migrate_organization_schema(engine)
    second = migrate_organization_schema(engine)
    inspector = inspect(engine)
    assert "plaud_files.folder_id" in first
    assert "plaud_files.local_title" in first
    assert second == []
    assert {"folders", "tags", "recording_tags"}.issubset(inspector.get_table_names())
    assert {"folder_id", "local_title"} <= {
        column["name"] for column in inspector.get_columns("plaud_files")
    }
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT id FROM plaud_files")) == "kept"


def test_crud_validation_conflicts_and_unknowns(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    assert client.post("/api/folders", json={"name": "   "}).status_code == 422
    assert client.post("/api/tags", json={"name": "x" * 81}).status_code == 422

    folder = client.post("/api/folders", json={"name": " Work ", "color": "blue"})
    assert folder.status_code == 201
    assert folder.json()["name"] == "Work"
    assert client.post("/api/folders", json={"name": "work"}).status_code == 409
    assert client.patch("/api/folders/999", json={"name": "Nope"}).status_code == 404
    renamed_folder = client.patch(
        f"/api/folders/{folder.json()['id']}",
        json={"name": "Projects", "color": folder.json()["color"]},
    )
    assert renamed_folder.json()["name"] == "Projects"
    assert renamed_folder.json()["color"] == "blue"

    tag = client.post("/api/tags", json={"name": "Person", "color": "red"}).json()
    changed = client.patch(f"/api/tags/{tag['id']}", json={"name": "People"})
    assert changed.json() == {"id": tag["id"], "name": "People", "color": None}
    assert client.delete("/api/tags/999").status_code == 404

    from localplaud.db.models import AutomationRule
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            AutomationRule(
                name="Keep organization references valid",
                trigger={"folder_id": folder.json()["id"]},
                actions={"add_tag_ids": [tag["id"]]},
            )
        )
        session.add(
            AutomationRule(
                name="Keep string organization references valid",
                trigger={"tag_id": str(tag["id"])},
                actions={"folder_id": str(folder.json()["id"])},
            )
        )
    blocked_folder = client.delete(f"/api/folders/{folder.json()['id']}")
    blocked_tag = client.delete(f"/api/tags/{tag['id']}")
    assert blocked_folder.status_code == 409
    assert blocked_folder.json()["detail"] == "folder is used by an AutoFlow rule"
    assert blocked_tag.status_code == 409
    assert blocked_tag.json()["detail"] == "tag is used by an AutoFlow rule"


def test_bulk_organization_is_atomic_and_supports_unassign_remove(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed_files()
    import localplaud.worker.knowledge_index as knowledge_index

    sync_order: list[str] = []
    monkeypatch.setattr(
        knowledge_index,
        "sync_file_knowledge_documents",
        lambda _session, file_id: sync_order.append(file_id),
    )
    folder = client.post("/api/folders", json={"name": "Work"}).json()
    tag1 = client.post("/api/tags", json={"name": "One"}).json()
    tag2 = client.post("/api/tags", json={"name": "Two"}).json()

    response = client.post(
        "/api/files/organize",
        json={"file_ids": ["b", "a"], "folder_id": folder["id"], "add_tag_ids": [tag2["id"], tag1["id"]]},
    )
    assert response.json() == {"updated": 2}
    assert sync_order == ["a", "b"]
    summary = {row["id"]: row for row in client.get("/api/files").json()["files"]}
    assert summary["a"]["folder"]["name"] == "Work"
    assert [tag["name"] for tag in summary["a"]["tags"]] == ["One", "Two"]

    # A missing reference rejects the whole request, including the valid file.
    assert client.post(
        "/api/files/organize",
        json={"file_ids": ["a", "missing"], "folder_id": None, "remove_tag_ids": [tag1["id"]]},
    ).status_code == 404
    unchanged = next(row for row in client.get("/api/files").json()["files"] if row["id"] == "a")
    assert unchanged["folder"] is not None
    assert {tag["id"] for tag in unchanged["tags"]} == {tag1["id"], tag2["id"]}

    assert client.post(
        "/api/files/organize",
        json={"file_ids": ["a"], "folder_id": None, "remove_tag_ids": [tag1["id"], tag2["id"]]},
    ).json() == {"updated": 1}
    cleared = next(row for row in client.get("/api/files").json()["files"] if row["id"] == "a")
    assert cleared["folder"] is None and cleared["tags"] == []
    assert client.post("/api/files/organize", json={"file_ids": ["a"]}).status_code == 422


def test_folder_mutations_reject_active_recording_claim(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    client = _client(monkeypatch, tmp_path)
    _seed_files()
    from localplaud.db.models import PlaudFile
    from localplaud.db.session import session_scope

    folder = client.post("/api/folders", json={"name": "Protected"}).json()
    with session_scope() as session:
        recording = session.get(PlaudFile, "a")
        recording.processing_token = "active-worker"
        recording.processing_lease_until = datetime.now(UTC) + timedelta(minutes=5)

    assign = client.post(
        "/api/files/organize",
        json={"file_ids": ["a"], "folder_id": folder["id"]},
    )
    assert assign.status_code == 409
    assert "processing" in assign.json()["detail"]
    with session_scope() as session:
        assert session.get(PlaudFile, "a").folder_id is None


def test_counts_filters_uncategorized_and_delete_cleanup(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed_files()
    folder = client.post("/api/folders", json={"name": "Work"}).json()
    tag = client.post("/api/tags", json={"name": "Topic"}).json()
    client.post(
        "/api/files/organize",
        json={"file_ids": ["a", "trash"], "folder_id": folder["id"], "add_tag_ids": [tag["id"]]},
    )
    org = client.get("/api/organization").json()
    assert org["folders"][0]["count"] == 1
    assert org["tags"][0]["count"] == 1
    assert [row["id"] for row in client.get(f"/api/files?folder={folder['id']}").json()["files"]] == ["a"]
    assert [row["id"] for row in client.get(f"/api/files?tag={tag['id']}").json()["files"]] == ["a"]
    assert [row["id"] for row in client.get("/api/files?view=uncategorized").json()["files"]] == ["b"]
    # Invalid organization filters retain the established default fallback.
    assert {row["id"] for row in client.get("/api/files?folder=no&tag=no").json()["files"]} == {"a", "b"}

    assert client.delete(f"/api/folders/{folder['id']}").status_code == 200
    assert client.delete(f"/api/tags/{tag['id']}").status_code == 200
    row = next(row for row in client.get("/api/files").json()["files"] if row["id"] == "a")
    assert row["folder"] is None and row["tags"] == []


def test_library_renders_organization_and_bulk_controls(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed_files()
    folder = client.post("/api/folders", json={"name": "Research"}).json()
    tag = client.post("/api/tags", json={"name": "Interview"}).json()
    client.post(
        "/api/files/organize",
        json={"file_ids": ["a"], "folder_id": folder["id"], "add_tag_ids": [tag["id"]]},
    )

    page = client.get("/")
    assert page.status_code == 200
    assert "Library organization" in page.text
    assert "Research" in page.text
    assert "Interview" in page.text
    assert "Uncategorized" in page.text
    assert 'id="bulkbar"' in page.text
    assert 'id="organization-manager-open"' in page.text
    assert 'id="organization-manager-backdrop" hidden' in page.text
    assert 'aria-labelledby="organization-manager-title"' in page.text
    assert 'data-organization-kind="folders"' in page.text
    assert 'data-organization-kind="tags"' in page.text
    assert 'aria-label="Rename Research"' in page.text
    assert 'aria-label="Delete Interview"' in page.text
    assert "const organizationModal = organizationBackdrop ? window.localplaudModal" in page.text
    assert "event.stopImmediatePropagation();organizationModal?.close()" in page.text
    assert "{capture:true,signal:cleanupController.signal}" in page.text
    assert "background:()=>[document.querySelector('.library-page')" in page.text
    assert "method:'PATCH'" in page.text
    assert "method:'DELETE'" in page.text
    assert "body:JSON.stringify({name:nextName,color})" in page.text
    assert "throw new Error(tr(fallback))" in page.text
    assert "const nextName=input.value.trim()" in page.text
    assert "String(new FormData(form).get('name') || '').trim()" in page.text
    assert "}, {signal:cleanupController.signal})" in page.text
    assert "if(url.searchParams.get(queryKey)===id)url.searchParams.delete(queryKey)" in page.text
    assert "window.alert(error.message)" not in page.text

    from localplaud.i18n import catalog

    messages = catalog("zh-Hant-TW")
    assert messages["Manage folders and tags"] == "管理資料夾與標籤"
    assert messages["name already exists"] == "名稱已存在"
    assert messages["Could not delete item"] == "無法刪除項目"
    assert '<option value="resume">' in page.text
    assert '<option value="delete-local-processing">' in page.text
    assert 'value="a"' in page.text

    detail = client.get("/file/a")
    assert detail.status_code == 200
    assert 'href="/?folder=' in detail.text and "Research" in detail.text
    assert 'href="/?tag=' in detail.text and "Interview" in detail.text
    assert 'id="edit-recording-metadata"' in detail.text
    assert 'id="metadata-form"' in detail.text
    assert f'value="{folder["id"]}" selected' in detail.text
    assert f'value="{tag["id"]}" checked' in detail.text
    assert f"currentTagIds=[{tag['id']}]" in detail.text

    trash = client.get("/?view=trash")
    assert 'id="bulkbar"' not in trash.text
    assert "read-only recovery view" in trash.text


def test_organization_membership_rejects_active_library_ask(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _seed_files()
    folder = client.post("/api/folders", json={"name": "Research"}).json()
    tag = client.post("/api/tags", json={"name": "Priority"}).json()
    assigned = client.post(
        "/api/files/organize",
        json={
            "file_ids": ["a"],
            "folder_id": folder["id"],
            "add_tag_ids": [tag["id"]],
        },
    )
    assert assigned.status_code == 200

    from localplaud.db.models import AskThread, PlaudFile
    from localplaud.db.session import session_scope

    with session_scope() as session:
        session.add(
            AskThread(
                id="scoped-ask",
                title="Scoped Ask",
                retrieval_scope={"tag_id": tag["id"]},
                request_token="active-request",
                request_lease_until=datetime.now(UTC) + timedelta(minutes=5),
            )
        )

    removed = client.post(
        "/api/files/organize",
        json={"file_ids": ["a"], "remove_tag_ids": [tag["id"]]},
    )
    assert removed.status_code == 409
    assert "used by Ask" in removed.json()["detail"]
    assert client.delete(f"/api/tags/{tag['id']}").status_code == 409
    assert client.delete(f"/api/folders/{folder['id']}").status_code == 409

    with session_scope() as session:
        row = session.get(PlaudFile, "a")
        assert row.folder_id == folder["id"]
        assert {item.id for item in row.tags} == {tag["id"]}
