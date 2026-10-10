"""Workspace isolation: one private library per account, enforced in the ORM."""

import pytest
from sqlalchemy import create_engine, delete, func, inspect, select, text, update
from sqlalchemy.orm import Session

from localplaud.db import tenancy
from localplaud.db.models import (
    AccountUser,
    Base,
    PlaudFile,
    Summary,
    Tag,
    Transcript,
    VocabularyTerm,
    Workspace,
    WorkspaceOwned,
)
from localplaud.db.tenancy import (
    WorkspaceViolation,
    local_file_id,
    system_scope,
    workspace_scope,
)


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tenancy.db'}")
    Base.metadata.create_all(engine)
    with system_scope(), Session(engine) as session:
        session.add(Workspace(id=2, name="other"))
        session.commit()
    for workspace_id, file_id in ((1, "mine"), (2, local_file_id(2, "theirs"))):
        with workspace_scope(workspace_id), Session(engine) as session:
            recording = PlaudFile(id=file_id, filename=f"{file_id}.mp3")
            recording.transcripts.append(Transcript(provider="test", text=f"{file_id} text"))
            recording.summaries.append(Summary(template="meeting", content_md=file_id))
            recording.tags.append(Tag(name="shared-name"))
            session.add(recording)
            session.add(VocabularyTerm(source_text="plaud", replacement_text="Plaud"))
            session.commit()
    yield engine
    engine.dispose()


def test_every_content_model_is_workspace_owned():
    unscoped = {
        "workspaces",
        "account_users",
        "oauth_identities",
        "auth_transactions",
        "auth_rate_limits",
        "account_audit_events",
        "browser_sessions",
        "provider_connections",
        "model_catalog_entries",
        "execution_profiles",
        "profile_stage_selections",
        "remote_workers",
        "remote_jobs",
        "backup_destinations",
        "backup_sync_deliveries",
        "kv",
        "plaud_connections",
        "voice_samples",
        "voice_assignments",
        "voice_identity_events",
    }
    for mapper in Base.registry.mappers:
        table = mapper.local_table.name
        if table in unscoped:
            continue
        assert issubclass(mapper.class_, WorkspaceOwned), (
            f"{table} must be WorkspaceOwned or deliberately listed as system data"
        )


def test_reads_relationships_and_get_stay_in_the_active_workspace(engine):
    theirs = local_file_id(2, "theirs")
    with workspace_scope(1), Session(engine) as session:
        assert session.scalars(select(PlaudFile.id)).all() == ["mine"]
        assert session.get(PlaudFile, theirs) is None
        assert session.scalar(select(func.count()).select_from(Transcript)) == 1
        assert [t.name for t in session.get(PlaudFile, "mine").tags] == ["shared-name"]
        assert session.scalar(select(func.count(Tag.id))) == 1
        joined = session.scalars(
            select(Summary).join(PlaudFile).where(PlaudFile.id == theirs)
        ).all()
        assert joined == []
    # The same compiled statement must not reuse the first workspace's value.
    for workspace_id, expected in ((2, [theirs]), (1, ["mine"]), (2, [theirs])):
        with workspace_scope(workspace_id), Session(engine) as session:
            assert session.scalars(select(PlaudFile.id)).all() == expected
    with system_scope(), Session(engine) as session:
        assert sorted(session.scalars(select(PlaudFile.id))) == sorted(["mine", theirs])


def test_bulk_update_and_delete_cannot_touch_another_workspace(engine):
    with workspace_scope(1), Session(engine) as session:
        session.execute(update(PlaudFile).values(filename="renamed"))
        session.execute(delete(Transcript))
        session.commit()
    with system_scope(), Session(engine) as session:
        names = dict(session.execute(select(PlaudFile.id, PlaudFile.filename)).all())
        assert names == {"mine": "renamed", local_file_id(2, "theirs"): "w2-theirs.mp3"}
        assert session.scalars(select(Transcript.text)).all() == ["w2-theirs text"]


def test_writes_are_stamped_and_cross_workspace_writes_refused(engine):
    with workspace_scope(2), Session(engine) as session:
        session.add(Tag(name="new"))
        session.commit()
        assert session.scalar(select(Tag.workspace_id).where(Tag.name == "new")) == 2
        session.add(Tag(name="smuggled", workspace_id=1))
        with pytest.raises(WorkspaceViolation):
            session.commit()
    # System code must place rows explicitly; children inherit their recording.
    with system_scope(), Session(engine) as session:
        session.add(Transcript(file_id=local_file_id(2, "theirs"), provider="t", text="x"))
        session.commit()
        assert session.scalar(select(Transcript.workspace_id).where(Transcript.text == "x")) == 2
        session.add(Tag(name="orphan"))
        with pytest.raises(WorkspaceViolation):
            session.commit()


def test_unscoped_code_defaults_to_the_original_workspace(engine):
    with Session(engine) as session:
        assert session.scalars(select(PlaudFile.id)).all() == ["mine"]


def test_scope_follows_new_threads_only_when_bound(engine):
    import threading

    seen = {}

    def read(key):
        with Session(engine) as session:
            seen[key] = session.scalars(select(PlaudFile.id)).all()

    with workspace_scope(2):
        plain = threading.Thread(target=read, args=("plain",))
        bound = threading.Thread(target=tenancy.run_in_current_workspace(read), args=("bound",))
        plain.start(), bound.start()
        plain.join(), bound.join()
    assert seen == {"plain": ["mine"], "bound": [local_file_id(2, "theirs")]}


def test_per_workspace_uniqueness(engine):
    with system_scope(), Session(engine) as session:
        assert session.scalar(select(func.count(VocabularyTerm.id))) == 2


def test_legacy_library_migrates_into_the_owner_workspace(tmp_path):
    from localplaud.db.migrations import migrate_workspaces

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE plaud_files (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO plaud_files (id) VALUES ('kept')"))
        connection.execute(
            text(
                "CREATE TABLE vocabulary_terms (id INTEGER PRIMARY KEY, source_text VARCHAR(300),"
                " replacement_text VARCHAR(300), language VARCHAR(24), case_sensitive BOOLEAN,"
                " enabled BOOLEAN, created_at DATETIME, updated_at DATETIME,"
                " CONSTRAINT uq_vocabulary_source_language UNIQUE (source_text, language))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO vocabulary_terms (id, source_text, replacement_text, case_sensitive, enabled, created_at, updated_at) VALUES (7, 'a', 'b', 0, 1, '2026-01-01', '2026-01-01')"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE account_users (id INTEGER PRIMARY KEY, username VARCHAR(64) UNIQUE,"
                " email VARCHAR(254) UNIQUE, password_hash TEXT, role VARCHAR(16), status VARCHAR(16),"
                " owner_slot VARCHAR(16) UNIQUE, created_at DATETIME,"
                " CHECK (role IN ('owner', 'admin', 'viewer')), CHECK (status IN ('pending', 'active', 'disabled')))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO account_users (id, username, email, role, status, owner_slot, created_at) VALUES"
                " (1, 'sky', 'o@example.com', 'owner', 'active', 'owner', '2026-01-01'),"
                " (2, 'old', 'v@example.com', 'viewer', 'active', NULL, '2026-01-01')"
            )
        )
        connection.execute(
            text("CREATE TABLE kv (key VARCHAR(128) PRIMARY KEY, value JSON, updated_at DATETIME)")
        )
        connection.execute(
            text(
                "INSERT INTO kv (key, value) VALUES ('workspace_preferences', "
                '\'{"workspace_name": "w80233781"}\')'
            )
        )
    Base.metadata.create_all(engine)
    # create_all seeds a placeholder workspace 1; the migration names and claims it.
    changed = migrate_workspaces(engine)
    assert "plaud_files.workspace_id" in changed
    assert "vocabulary_terms.unique" in changed
    assert "account_users.role" in changed
    assert migrate_workspaces(engine) == []
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id, workspace_id FROM plaud_files")).all() == [
            ("kept", 1)
        ]
        assert connection.execute(text("SELECT id, workspace_id FROM vocabulary_terms")).all() == [
            (7, 1)
        ]
        assert connection.execute(text("SELECT id, name, owner_user_id FROM workspaces")).all() == [
            (1, "sky", 1)
        ]
        assert dict(connection.execute(text("SELECT username, role FROM account_users")).all()) == {
            "sky": "owner",
            "old": "member",
        }
    constraints = inspect(engine).get_unique_constraints("vocabulary_terms")
    assert constraints[0]["column_names"] == ["workspace_id", "source_text", "language"]
    with Session(engine) as session:
        assert session.get(AccountUser, 2).role == "member"
