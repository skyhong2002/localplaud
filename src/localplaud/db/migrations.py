"""Small idempotent data migrations that do not need an external migration tool."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from .models import Chunk, FileStatus, KeyValue, PlaudFile, Transcript

INDEPENDENT_MIGRATION_KEY = "migration.independent-artifacts.v1"
_PLAUD_SOURCES = {"cloud", "plaud"}


def migrate_legacy_provider_profile_schema(engine: Engine) -> list[str]:
    """Rebuild the pre-contract provider/profile tables without changing row IDs.

    An early deployed schema stored connection configuration and whole-profile JSON
    directly on these tables. SQLite cannot drop its legacy NOT NULL columns, so an
    additive migration would still break future inserts from the current ORM.
    """
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if not {"provider_connections", "execution_profiles"} <= tables:
        return []
    connection_columns = {
        column["name"] for column in inspector.get_columns("provider_connections")
    }
    profile_columns = {
        column["name"] for column in inspector.get_columns("execution_profiles")
    }
    if "configuration" not in connection_columns or "stages" not in profile_columns:
        return []

    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.executescript("""
            BEGIN;
            CREATE TABLE provider_connections_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                key VARCHAR(64) NOT NULL UNIQUE,
                name VARCHAR(128) NOT NULL,
                provider_type VARCHAR(64) NOT NULL,
                execution_target VARCHAR(32) NOT NULL DEFAULT 'local',
                data_egress BOOLEAN NOT NULL DEFAULT 0,
                secret_ref VARCHAR(256),
                config JSON NOT NULL DEFAULT '{}',
                health JSON NOT NULL DEFAULT '{}',
                created_at DATETIME NOT NULL
            );
            INSERT INTO provider_connections_new (
                id, key, name, provider_type, execution_target, data_egress,
                secret_ref, config, health, created_at
            )
            SELECT
                id,
                name,
                name,
                provider_type,
                CASE
                    WHEN provider_type = 'remote-worker' THEN 'remote_worker'
                    WHEN provider_type IN ('openai', 'deepgram', 'assemblyai', 'anthropic')
                        THEN 'cloud'
                    ELSE 'local'
                END,
                CASE
                    WHEN provider_type IN (
                        'remote-worker', 'openai', 'deepgram', 'assemblyai', 'anthropic'
                    ) THEN 1 ELSE 0
                END,
                secret_ref,
                CASE
                    WHEN base_url IS NOT NULL AND base_url != ''
                        THEN json_set(COALESCE(configuration, '{}'), '$.base_url', base_url)
                    ELSE COALESCE(configuration, '{}')
                END,
                '{}',
                created_at
            FROM provider_connections;
            DROP TABLE provider_connections;
            ALTER TABLE provider_connections_new RENAME TO provider_connections;

            CREATE TABLE execution_profiles_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                key VARCHAR(64) NOT NULL,
                name VARCHAR(128) NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                is_system_default BOOLEAN NOT NULL DEFAULT 0,
                privacy_policy VARCHAR(32) NOT NULL DEFAULT 'allow-egress',
                no_egress BOOLEAN NOT NULL DEFAULT 0,
                cost_ceiling FLOAT,
                fallback_policy JSON NOT NULL DEFAULT '{}',
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_profile_key_version UNIQUE (key, version)
            );
            INSERT INTO execution_profiles_new (
                id, key, name, version, is_system_default, privacy_policy,
                no_egress, cost_ceiling, fallback_policy, created_at
            )
            SELECT
                id,
                CASE WHEN is_system_default = 1 THEN 'legacy-settings-default' ELSE name END,
                name,
                version,
                is_system_default,
                CASE
                    WHEN COALESCE(json_extract(policy, '$.no_egress'), 0) = 1
                        THEN 'local-only'
                    ELSE 'allow-egress'
                END,
                COALESCE(json_extract(policy, '$.no_egress'), 0),
                json_extract(policy, '$.cost_ceiling'),
                COALESCE(json_extract(policy, '$.fallback_policy'), '{}'),
                created_at
            FROM execution_profiles;
            DROP TABLE execution_profiles;
            ALTER TABLE execution_profiles_new RENAME TO execution_profiles;
        """)
        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"legacy profile migration broke foreign keys: {violations}")
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    return ["provider_connections", "execution_profiles"]


def migrate_legacy_note_template_schema(engine: Engine) -> list[str]:
    """Rebuild the deployed pre-versioned note-template table in place."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "note_templates" not in set(inspector.get_table_names()):
        return []
    columns = {column["name"] for column in inspector.get_columns("note_templates")}
    if "key" in columns or not {"name", "system_prompt", "instructions"} <= columns:
        return []

    def legacy(column: str, default: str = "NULL") -> str:
        return column if column in columns else default

    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        unsupported = [
            column
            for column in ("language", "execution_profile_id")
            if column in columns
            and cursor.execute(
                f"SELECT 1 FROM note_templates WHERE {column} IS NOT NULL LIMIT 1"
            ).fetchone()
        ]
        if unsupported:
            raise RuntimeError(
                "legacy note-template migration cannot preserve non-empty columns: "
                + ", ".join(unsupported)
            )
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.executescript(f"""
            BEGIN;
            CREATE TABLE note_templates_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                key VARCHAR(64) NOT NULL,
                version INTEGER NOT NULL,
                name VARCHAR(80) NOT NULL,
                system_prompt TEXT NOT NULL,
                instructions TEXT NOT NULL,
                category VARCHAR(80),
                scenario VARCHAR(80),
                description VARCHAR(512),
                author VARCHAR(120),
                provenance VARCHAR(32),
                popularity INTEGER,
                is_builtin BOOLEAN NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_note_template_key_version UNIQUE (key, version)
            );
            INSERT INTO note_templates_new (
                id, key, version, name, system_prompt, instructions, category,
                scenario, description, author, provenance, popularity,
                is_builtin, is_active, created_at
            )
            SELECT
                id,
                lower(replace(trim(name), ' ', '-')),
                COALESCE({legacy('version', '1')}, 1),
                name,
                system_prompt,
                instructions,
                {legacy('category')},
                {legacy('scenario')},
                {legacy('description')},
                {legacy('author')},
                {legacy('provenance')},
                {legacy('popularity')},
                CASE WHEN {legacy('provenance')} = 'builtin' THEN 1 ELSE 0 END,
                COALESCE({legacy('enabled', '1')}, 1),
                {legacy('created_at', 'CURRENT_TIMESTAMP')}
            FROM note_templates;
            DROP TABLE note_templates;
            ALTER TABLE note_templates_new RENAME TO note_templates;
            CREATE INDEX ix_note_templates_key ON note_templates (key);
            CREATE INDEX ix_note_templates_is_active ON note_templates (is_active);
        """)
        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"legacy note-template migration broke foreign keys: {violations}")
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.cursor().execute("PRAGMA foreign_keys=ON")
        raw.close()
    return ["note_templates"]


def migrate_legacy_summary_schema(engine: Engine) -> list[str]:
    """Remove obsolete NOT NULL summary columns from deployed SQLite databases.

    Early releases stored ``revision``, ``transcript_revision``, and
    ``profile_snapshot`` directly on summaries.  The current schema replaced
    those fields with explicit canonical-transcript lineage and an immutable
    resolved profile snapshot.  SQLite keeps removed NOT NULL columns after an
    ORM-only upgrade, which makes every current insert fail unless the table is
    rebuilt.
    """
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "summaries" not in set(inspector.get_table_names()):
        return []
    columns = {column["name"] for column in inspector.get_columns("summaries")}
    legacy_columns = {"revision", "transcript_revision", "profile_snapshot"}
    if not columns & legacy_columns:
        return []

    def legacy(column: str, default: str = "NULL") -> str:
        return column if column in columns else default

    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.executescript(f"""
            BEGIN;
            CREATE TABLE summaries_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                file_id VARCHAR(64) NOT NULL,
                template VARCHAR(64) NOT NULL,
                template_version INTEGER,
                template_snapshot JSON,
                title VARCHAR(512),
                content_md TEXT NOT NULL,
                llm_provider VARCHAR(64),
                model VARCHAR(128),
                source VARCHAR(16) NOT NULL,
                input_transcript_id INTEGER,
                input_transcript_revision INTEGER,
                input_transcript_source VARCHAR(16),
                resolved_profile_snapshot JSON,
                created_at DATETIME NOT NULL,
                FOREIGN KEY(file_id) REFERENCES plaud_files (id) ON DELETE CASCADE,
                CONSTRAINT uq_summary_file_template UNIQUE (file_id, template)
            );
            INSERT INTO summaries_new (
                id, file_id, template, template_version, template_snapshot,
                title, content_md, llm_provider, model, source,
                input_transcript_id, input_transcript_revision,
                input_transcript_source, resolved_profile_snapshot, created_at
            )
            SELECT
                id, file_id, template, {legacy('template_version')},
                {legacy('template_snapshot')}, title, content_md, llm_provider,
                model, source, {legacy('input_transcript_id')},
                COALESCE({legacy('input_transcript_revision')},
                         {legacy('transcript_revision')}),
                {legacy('input_transcript_source')},
                COALESCE({legacy('resolved_profile_snapshot')},
                         {legacy('profile_snapshot')}),
                created_at
            FROM summaries;
            DROP TABLE summaries;
            ALTER TABLE summaries_new RENAME TO summaries;
        """)
        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"legacy summary migration broke foreign keys: {violations}")
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    return ["summaries"]


def migrate_legacy_stage_run_schema(engine: Engine) -> list[str]:
    """Rebuild the first deployed stage-run table to the current ORM contract."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "stage_runs" not in set(inspector.get_table_names()):
        return []
    columns = {column["name"] for column in inspector.get_columns("stage_runs")}
    legacy_columns = {"profile_snapshot", "latency_ms", "usage", "estimated_cost", "actual_cost"}
    if not columns & legacy_columns:
        return []

    def legacy(column: str, default: str = "NULL") -> str:
        return column if column in columns else default

    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.executescript(f"""
            BEGIN;
            CREATE TABLE stage_runs_new (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                file_id VARCHAR(64) NOT NULL,
                stage VARCHAR(32) NOT NULL,
                status VARCHAR(20) NOT NULL,
                attempts INTEGER NOT NULL,
                provider VARCHAR(64),
                model VARCHAR(128),
                artifact_source VARCHAR(32),
                detail JSON NOT NULL,
                resolved_profile_snapshot JSON,
                error TEXT,
                started_at DATETIME,
                completed_at DATETIME,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(file_id) REFERENCES plaud_files (id) ON DELETE CASCADE,
                CONSTRAINT uq_stage_run_file_stage UNIQUE (file_id, stage)
            );
            INSERT INTO stage_runs_new (
                id, file_id, stage, status, attempts, provider, model,
                artifact_source, detail, resolved_profile_snapshot, error,
                started_at, completed_at, updated_at
            )
            SELECT
                id, file_id, stage, status, attempts, provider, model,
                artifact_source, COALESCE(detail, '{{}}'),
                COALESCE({legacy('resolved_profile_snapshot')},
                         {legacy('profile_snapshot')}),
                error, started_at, completed_at, updated_at
            FROM stage_runs;
            DROP TABLE stage_runs;
            ALTER TABLE stage_runs_new RENAME TO stage_runs;
        """)
        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"legacy stage-run migration broke foreign keys: {violations}")
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
    return ["stage_runs"]


def migrate_automation_ownership_schema(engine: Engine) -> list[str]:
    """Add explicit local/external ownership to existing AutoFlow rules."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "automation_rules" not in set(inspector.get_table_names()):
        return []
    columns = {column["name"] for column in inspector.get_columns("automation_rules")}
    migrated: list[str] = []
    with engine.begin() as connection:
        for column, ddl in (
            ("owner_type", "VARCHAR(16) NOT NULL DEFAULT 'local'"),
            ("owner_key", "VARCHAR(64)"),
            ("owner_label", "VARCHAR(120)"),
            ("external_id", "VARCHAR(128)"),
            ("owner_detail", "JSON NOT NULL DEFAULT '{}'"),
        ):
            if column not in columns:
                connection.execute(
                    text(f"ALTER TABLE automation_rules ADD COLUMN {column} {ddl}")
                )
                migrated.append(f"automation_rules.{column}")
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_automation_rules_owner_type "
                "ON automation_rules (owner_type)"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_automation_rule_owner_external "
                "ON automation_rules (owner_key, external_id)"
            )
        )
    return migrated


def migrate_organization_schema(engine: Engine) -> list[str]:
    """Add local folder/tag metadata to an existing SQLite library."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    migrated: list[str] = []
    with engine.begin() as connection:
        if "plaud_files" in existing:
            columns = {column["name"] for column in inspector.get_columns("plaud_files")}
            if "local_title" not in columns:
                connection.execute(
                    text("ALTER TABLE plaud_files ADD COLUMN local_title VARCHAR(512)")
                )
                migrated.append("plaud_files.local_title")
            if "folder_id" not in columns:
                connection.execute(
                    text("ALTER TABLE plaud_files ADD COLUMN folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL")
                )
                migrated.append("plaud_files.folder_id")
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS folders (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(80) NOT NULL,
                color VARCHAR(64),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(80) NOT NULL,
                color VARCHAR(64),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS recording_tags (
                file_id VARCHAR(64) NOT NULL REFERENCES plaud_files(id) ON DELETE CASCADE,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (file_id, tag_id)
            )
        """))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_plaud_files_folder_id ON plaud_files (folder_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_recording_tags_tag_id ON recording_tags (tag_id)"))
    for table in ("folders", "tags", "recording_tags"):
        if table not in existing:
            migrated.append(table)
    return migrated


def migrate_note_template_schema(engine: Engine) -> list[str]:
    """Add editable-note-template metadata to an existing SQLite library."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    migrated: list[str] = []
    with engine.begin() as connection:
        for table, column, ddl in (
            ("plaud_files", "note_template_key", "VARCHAR(64)"),
            ("summaries", "template_version", "INTEGER"),
            ("summaries", "template_snapshot", "JSON"),
            ("note_templates", "category", "VARCHAR(80)"),
            ("note_templates", "scenario", "VARCHAR(80)"),
            ("note_templates", "description", "VARCHAR(512)"),
            ("note_templates", "author", "VARCHAR(120)"),
            ("note_templates", "provenance", "VARCHAR(32)"),
            ("note_templates", "popularity", "INTEGER"),
        ):
            if table not in tables:
                continue
            columns = {item["name"] for item in inspector.get_columns(table)}
            if column not in columns:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                migrated.append(f"{table}.{column}")
    return migrated


def migrate_artifact_lineage_columns(engine: Engine) -> list[str]:
    """Add canonical transcript lineage to derived artifacts."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    migrated: list[str] = []
    with engine.begin() as connection:
        for table in ("summaries", "chunks"):
            if table not in tables:
                continue
            columns = {item["name"] for item in inspector.get_columns(table)}
            for column, ddl in (
                ("input_transcript_id", "INTEGER"),
                ("input_transcript_revision", "INTEGER"),
                ("input_transcript_source", "VARCHAR(16)"),
            ):
                if column not in columns:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    )
                    migrated.append(f"{table}.{column}")
    return migrated


def migrate_import_schema(engine: Engine) -> list[str]:
    """Add recording origin to existing SQLite libraries."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "plaud_files" not in inspector.get_table_names():
        return []
    columns = {item["name"] for item in inspector.get_columns("plaud_files")}
    if "origin" in columns:
        return []
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE plaud_files ADD COLUMN origin "
                "VARCHAR(32) NOT NULL DEFAULT 'plaud'"
            )
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_plaud_files_origin ON plaud_files (origin)")
        )
    return ["plaud_files.origin"]


def migrate_pipeline_retry_schema(engine: Engine) -> list[str]:
    """Add durable pipeline retry scheduling to an existing SQLite library."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "plaud_files" not in inspector.get_table_names():
        return []
    columns = {item["name"] for item in inspector.get_columns("plaud_files")}
    migrated: list[str] = []
    with engine.begin() as connection:
        for column, ddl in (
            ("pipeline_retry_count", "INTEGER NOT NULL DEFAULT 0"),
            ("pipeline_next_retry_at", "DATETIME"),
            ("pipeline_last_failure_at", "DATETIME"),
        ):
            if column not in columns:
                connection.execute(text(f"ALTER TABLE plaud_files ADD COLUMN {column} {ddl}"))
                migrated.append(f"plaud_files.{column}")
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_plaud_files_pipeline_next_retry_at "
                "ON plaud_files (pipeline_next_retry_at)"
            )
        )
    return migrated


def migrate_processing_claim_schema(engine: Engine) -> list[str]:
    """Add the durable per-recording worker claim to legacy libraries."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "plaud_files" not in inspector.get_table_names():
        return []
    columns = {item["name"] for item in inspector.get_columns("plaud_files")}
    migrated: list[str] = []
    with engine.begin() as connection:
        for column, ddl in (
            ("processing_token", "VARCHAR(64)"),
            ("processing_lease_until", "DATETIME"),
        ):
            if column not in columns:
                connection.execute(text(f"ALTER TABLE plaud_files ADD COLUMN {column} {ddl}"))
                migrated.append(f"plaud_files.{column}")
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_plaud_files_processing_token
            ON plaud_files (processing_token)
        """))
        connection.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_plaud_files_processing_lease_until
            ON plaud_files (processing_lease_until)
        """))
    return migrated


def migrate_stage_attempt_schema(engine: Engine) -> list[str]:
    """Create the append-only stage usage ledger for an existing library."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "stage_attempts" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("stage_attempts")}
        expected = {"resolved_profile_snapshot", "estimated_cost_usd"}
        if expected <= columns:
            return []

        # The first deployed ledger used different names and retained required
        # detail/profile columns. Rebuild it because merely adding the new columns
        # would leave current ORM inserts failing those legacy NOT NULL constraints.
        def legacy(column: str, default: str = "NULL") -> str:
            return column if column in columns else default

        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.executescript(f"""
                BEGIN;
                CREATE TABLE stage_attempts_new (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    file_id VARCHAR(64) NOT NULL REFERENCES plaud_files(id) ON DELETE CASCADE,
                    stage VARCHAR(32) NOT NULL,
                    attempt INTEGER NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    provider VARCHAR(64),
                    model VARCHAR(128),
                    resolved_profile_snapshot JSON,
                    usage JSON NOT NULL DEFAULT '{{}}',
                    estimated_cost_usd FLOAT NOT NULL DEFAULT 0,
                    latency_ms BIGINT,
                    error TEXT,
                    started_at DATETIME NOT NULL,
                    completed_at DATETIME,
                    CONSTRAINT uq_stage_attempt_number UNIQUE (file_id, stage, attempt)
                );
                INSERT INTO stage_attempts_new (
                    id, file_id, stage, attempt, status, provider, model,
                    resolved_profile_snapshot, usage, estimated_cost_usd,
                    latency_ms, error, started_at, completed_at
                )
                SELECT
                    id, file_id, stage, attempt, status, provider, model,
                    COALESCE({legacy('resolved_profile_snapshot')}, {legacy('profile_snapshot')}),
                    COALESCE({legacy('usage', "'{}'")}, '{{}}'),
                    COALESCE({legacy('estimated_cost_usd')}, {legacy('estimated_cost', '0')}, 0),
                    {legacy('latency_ms')}, {legacy('error')}, started_at, {legacy('completed_at')}
                FROM stage_attempts;
                DROP TABLE stage_attempts;
                ALTER TABLE stage_attempts_new RENAME TO stage_attempts;
                CREATE INDEX ix_stage_attempts_file_id ON stage_attempts (file_id);
            """)
            violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"legacy stage-attempt migration broke foreign keys: {violations}"
                )
            raw.commit()
            cursor.execute("PRAGMA foreign_keys=ON")
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()
        return ["stage_attempts"]
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE stage_attempts (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                file_id VARCHAR(64) NOT NULL REFERENCES plaud_files(id) ON DELETE CASCADE,
                stage VARCHAR(32) NOT NULL,
                attempt INTEGER NOT NULL,
                status VARCHAR(20) NOT NULL,
                provider VARCHAR(64),
                model VARCHAR(128),
                resolved_profile_snapshot JSON,
                usage JSON NOT NULL DEFAULT '{}',
                estimated_cost_usd FLOAT NOT NULL DEFAULT 0,
                latency_ms BIGINT,
                error TEXT,
                started_at DATETIME NOT NULL,
                completed_at DATETIME,
                CONSTRAINT uq_stage_attempt_number UNIQUE (file_id, stage, attempt)
            )
        """))
        connection.execute(
            text("CREATE INDEX ix_stage_attempts_file_id ON stage_attempts (file_id)")
        )
    return ["stage_attempts"]


def migrate_ask_provenance_schema(engine: Engine) -> list[str]:
    """Add provider/profile/usage provenance and durable retrieval scope to Ask."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if not {"ask_messages", "ask_threads"} & tables:
        return []

    message_columns = (
        {item["name"] for item in inspector.get_columns("ask_messages")}
        if "ask_messages" in tables
        else set()
    )
    thread_columns = (
        {item["name"] for item in inspector.get_columns("ask_threads")}
        if "ask_threads" in tables
        else set()
    )
    legacy_ask = bool(
        {"citations", "profile_snapshot", "estimated_cost", "actual_cost"}
        & message_columns
    ) or (
        "ask_threads" in tables
        and str(next(
            item["type"]
            for item in inspector.get_columns("ask_threads")
            if item["name"] == "id"
        )).upper().startswith("INTEGER")
    )
    if legacy_ask and {"ask_messages", "ask_threads"} <= tables:
        def legacy(columns: set[str], column: str, default: str = "NULL") -> str:
            return column if column in columns else default

        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.executescript(f"""
                BEGIN;
                CREATE TABLE ask_threads_new (
                    id VARCHAR(36) NOT NULL PRIMARY KEY,
                    file_id VARCHAR(64) REFERENCES plaud_files(id) ON DELETE CASCADE,
                    title VARCHAR(200) NOT NULL,
                    retrieval_scope JSON NOT NULL DEFAULT '{{}}',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                );
                INSERT INTO ask_threads_new (
                    id, file_id, title, retrieval_scope, created_at, updated_at
                )
                SELECT
                    CAST(id AS TEXT), file_id, COALESCE(title, 'Ask thread'),
                    COALESCE({legacy(thread_columns, 'retrieval_scope', "'{}'")}, '{{}}'),
                    {legacy(thread_columns, 'created_at', 'CURRENT_TIMESTAMP')},
                    {legacy(thread_columns, 'updated_at', 'CURRENT_TIMESTAMP')}
                FROM ask_threads;

                CREATE TABLE ask_messages_new (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    thread_id VARCHAR(36) NOT NULL
                        REFERENCES ask_threads_new(id) ON DELETE CASCADE,
                    role VARCHAR(16) NOT NULL,
                    content TEXT NOT NULL,
                    sources JSON NOT NULL DEFAULT '[]',
                    provider VARCHAR(64),
                    model VARCHAR(128),
                    resolved_profile_snapshot JSON,
                    usage JSON NOT NULL DEFAULT '{{}}',
                    estimated_cost_usd FLOAT NOT NULL DEFAULT 0,
                    skill_key VARCHAR(64),
                    skill_snapshot JSON,
                    created_at DATETIME NOT NULL
                );
                INSERT INTO ask_messages_new (
                    id, thread_id, role, content, sources, provider, model,
                    resolved_profile_snapshot, usage, estimated_cost_usd,
                    skill_key, skill_snapshot, created_at
                )
                SELECT
                    id, CAST(thread_id AS TEXT), role, content,
                    COALESCE(
                        {legacy(message_columns, 'sources')},
                        {legacy(message_columns, 'citations', "'[]'")}, '[]'
                    ),
                    {legacy(message_columns, 'provider')},
                    {legacy(message_columns, 'model')},
                    COALESCE(
                        {legacy(message_columns, 'resolved_profile_snapshot')},
                        {legacy(message_columns, 'profile_snapshot')}
                    ),
                    COALESCE({legacy(message_columns, 'usage', "'{}'")}, '{{}}'),
                    COALESCE(
                        {legacy(message_columns, 'estimated_cost_usd')},
                        {legacy(message_columns, 'estimated_cost', '0')}, 0
                    ),
                    {legacy(message_columns, 'skill_key')},
                    {legacy(message_columns, 'skill_snapshot')},
                    {legacy(message_columns, 'created_at', 'CURRENT_TIMESTAMP')}
                FROM ask_messages;

                DROP TABLE ask_messages;
                DROP TABLE ask_threads;
                ALTER TABLE ask_threads_new RENAME TO ask_threads;
                ALTER TABLE ask_messages_new RENAME TO ask_messages;
                CREATE INDEX ix_ask_threads_file_id ON ask_threads (file_id);
                CREATE INDEX ix_ask_messages_thread_id ON ask_messages (thread_id);
            """)
            violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"legacy Ask migration broke foreign keys: {violations}")
            raw.commit()
            cursor.execute("PRAGMA foreign_keys=ON")
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.cursor().execute("PRAGMA foreign_keys=ON")
            raw.close()
        return ["ask_threads", "ask_messages"]

    migrated: list[str] = []
    with engine.begin() as connection:
        if "ask_messages" in tables:
            for column, ddl in (
                ("provider", "VARCHAR(64)"),
                ("model", "VARCHAR(128)"),
                ("resolved_profile_snapshot", "JSON"),
                ("usage", "JSON NOT NULL DEFAULT '{}'"),
                ("estimated_cost_usd", "FLOAT NOT NULL DEFAULT 0"),
                ("skill_key", "VARCHAR(64)"),
                ("skill_snapshot", "JSON"),
            ):
                if column not in message_columns:
                    connection.execute(
                        text(f"ALTER TABLE ask_messages ADD COLUMN {column} {ddl}")
                    )
                    migrated.append(f"ask_messages.{column}")
        if "ask_threads" in tables:
            if "retrieval_scope" not in thread_columns:
                connection.execute(
                    text(
                        "ALTER TABLE ask_threads ADD COLUMN "
                        "retrieval_scope JSON NOT NULL DEFAULT '{}'"
                    )
                )
                migrated.append("ask_threads.retrieval_scope")
    return migrated


def migrate_editable_note_source_schema(engine: Engine) -> list[str]:
    """Link editable copies back to their immutable generated summary."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "user_notes" not in set(inspector.get_table_names()):
        return []
    columns = {item["name"] for item in inspector.get_columns("user_notes")}
    if "source_summary_id" in columns:
        return []
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE user_notes ADD COLUMN source_summary_id INTEGER")
        )
        connection.execute(text("""
            CREATE UNIQUE INDEX uq_user_notes_source_summary_id
            ON user_notes (source_summary_id)
            WHERE source_summary_id IS NOT NULL
        """))
    return ["user_notes.source_summary_id"]


def migrate_speaker_timeline_schema(engine: Engine) -> list[str]:
    """Add durable diarization evidence for stable speaker reconciliation."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "speakers" not in set(inspector.get_table_names()):
        return []
    columns = {item["name"] for item in inspector.get_columns("speakers")}
    if "timeline" in columns:
        return []
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE speakers ADD COLUMN timeline JSON"))
    return ["speakers.timeline"]


def migrate_vocabulary_schema(engine: Engine) -> list[str]:
    """Create the durable custom-vocabulary table for an existing library."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "vocabulary_terms" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("vocabulary_terms")}
        if "source_text" in columns:
            return []
        if not {"term", "replacement"} <= columns:
            return []
        raw = engine.raw_connection()
        try:
            cursor = raw.cursor()
            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.executescript("""
                BEGIN;
                CREATE TABLE vocabulary_terms_new (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    source_text VARCHAR(300) NOT NULL,
                    replacement_text VARCHAR(300) NOT NULL,
                    language VARCHAR(24),
                    case_sensitive BOOLEAN NOT NULL DEFAULT 0,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    CONSTRAINT uq_vocabulary_source_language UNIQUE (source_text, language)
                );
                INSERT INTO vocabulary_terms_new (
                    id, source_text, replacement_text, language, case_sensitive,
                    enabled, created_at, updated_at
                )
                SELECT id, term, replacement, language, case_sensitive, enabled,
                       created_at, updated_at
                FROM vocabulary_terms;
                DROP TABLE vocabulary_terms;
                ALTER TABLE vocabulary_terms_new RENAME TO vocabulary_terms;
                CREATE INDEX ix_vocabulary_terms_enabled ON vocabulary_terms (enabled);
            """)
            violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"legacy vocabulary migration broke foreign keys: {violations}")
            raw.commit()
            cursor.execute("PRAGMA foreign_keys=ON")
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()
        return ["vocabulary_terms"]
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE vocabulary_terms (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                source_text VARCHAR(300) NOT NULL,
                replacement_text VARCHAR(300) NOT NULL,
                language VARCHAR(24),
                case_sensitive BOOLEAN NOT NULL DEFAULT 0,
                enabled BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_vocabulary_source_language UNIQUE (source_text, language)
            )
        """))
        connection.execute(
            text("CREATE INDEX ix_vocabulary_terms_enabled ON vocabulary_terms (enabled)")
        )
    return ["vocabulary_terms"]


def migrate_profile_snapshot_columns(engine: Engine) -> list[str]:
    """Add immutable profile provenance to existing SQLite artifact tables."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    migrated: list[str] = []
    for table in ("stage_runs", "transcripts", "summaries", "chunks"):
        if table not in existing:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "resolved_profile_snapshot" in columns:
            continue
        with engine.begin() as connection:
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN resolved_profile_snapshot JSON")
            )
        migrated.append(table)
    return migrated


def migrate_stage_run_snapshot_column(engine: Engine) -> bool:
    """Backward-compatible wrapper for the original single-column migration."""
    return "stage_runs" in migrate_profile_snapshot_columns(engine)


def migrate_local_transcript_uniqueness(engine: Engine) -> list[str]:
    """Keep one canonical local raw transcript while preserving cloud imports.

    Early pipeline versions persisted both the pre-diarization and diarized local
    transcript. Corrections are separate immutable revisions, so duplicate raw
    rows are not history. Keep the newest row, detach revisions from discarded
    raw IDs, and enforce the invariant with a partial SQLite index.
    """
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "transcripts" not in inspector.get_table_names():
        return []
    index_name = "uq_transcripts_one_local_per_file"
    indexes = {index["name"] for index in inspector.get_indexes("transcripts")}
    if index_name in indexes:
        return []

    with engine.begin() as connection:
        if "transcript_revisions" in inspector.get_table_names():
            connection.execute(text("""
                UPDATE transcript_revisions
                SET base_transcript_id = NULL
                WHERE base_transcript_id IN (
                    SELECT old.id
                    FROM transcripts AS old
                    WHERE old.source = 'local'
                      AND old.id != (
                          SELECT MAX(newest.id)
                          FROM transcripts AS newest
                          WHERE newest.file_id = old.file_id
                            AND newest.source = 'local'
                      )
                )
            """))
        connection.execute(text("""
            DELETE FROM transcripts
            WHERE source = 'local'
              AND id != (
                  SELECT MAX(newest.id)
                  FROM transcripts AS newest
                  WHERE newest.file_id = transcripts.file_id
                    AND newest.source = 'local'
              )
        """))
        connection.execute(text(f"""
            CREATE UNIQUE INDEX {index_name}
            ON transcripts (file_id)
            WHERE source = 'local'
        """))
    return ["transcripts.local"]


def migrate_transcript_revision_provenance(engine: Engine) -> list[str]:
    """Add AI/deterministic correction provenance to legacy revision rows."""
    if engine.dialect.name != "sqlite":
        return []
    inspector = inspect(engine)
    if "transcript_revisions" not in inspector.get_table_names():
        return []
    columns = {
        column["name"] for column in inspector.get_columns("transcript_revisions")
    }
    migrated: list[str] = []
    with engine.begin() as connection:
        for column, ddl in (
            ("kind", "VARCHAR(32) NOT NULL DEFAULT 'user_edit'"),
            ("provider", "VARCHAR(64)"),
            ("model", "VARCHAR(128)"),
            ("prompt_version", "VARCHAR(64)"),
            ("resolved_profile_snapshot", "JSON"),
        ):
            if column not in columns:
                connection.execute(
                    text(f"ALTER TABLE transcript_revisions ADD COLUMN {column} {ddl}")
                )
                migrated.append(f"transcript_revisions.{column}")
    return migrated


def _legacy_template(template: str, used: set[str], row_id: int) -> str:
    """Return a unique <=64-char template name for preserved legacy notes."""
    prefix = "legacy-cloud-"
    candidate = f"{prefix}{template}"[:64]
    if candidate not in used:
        return candidate
    suffix = f"-{row_id}"
    return f"{candidate[: 64 - len(suffix)]}{suffix}"


def prepare_independent_mode(engine: Engine, *, force: bool = False) -> dict[str, int]:
    """Make legacy cloud-derived rows safe for raw-audio-only processing.

    Plaud transcripts are preserved alongside future local transcripts. Files that
    have only a Plaud transcript are requeued when their audio still exists. Local
    summaries made from those transcripts are retained but relabelled as legacy so
    they cannot satisfy a local summary stage; their non-provenanced chunks are
    discarded for regeneration from the future canonical local transcript.

    The marker keeps normal startup cheap and prevents repeatedly retrying genuine
    pipeline errors. Importing another cloud transcript clears the marker.
    """
    counts = {"files": 0, "summaries": 0, "chunks": 0, "requeued": 0}
    with Session(engine) as session:
        marker = session.get(KeyValue, INDEPENDENT_MIGRATION_KEY)
        if marker is not None and not force:
            return counts

        cloud_file_ids = set(
            session.scalars(select(Transcript.file_id).where(Transcript.source.in_(_PLAUD_SOURCES)))
        )
        local_file_ids = set(
            session.scalars(select(Transcript.file_id).where(Transcript.source == "local"))
        )
        affected = cloud_file_ids - local_file_ids

        for file_id in affected:
            file = session.get(PlaudFile, file_id)
            if file is None:
                continue
            counts["files"] += 1
            used_templates = {summary.template for summary in file.summaries}
            for summary in file.summaries:
                if summary.source != "local":
                    continue
                renamed = _legacy_template(summary.template, used_templates, summary.id)
                used_templates.add(renamed)
                summary.template = renamed
                summary.source = "legacy"
                counts["summaries"] += 1

            deleted = session.execute(delete(Chunk).where(Chunk.file_id == file_id)).rowcount
            counts["chunks"] += int(deleted or 0)

            if file.audio_path and Path(file.audio_path).exists():
                file.status = FileStatus.downloaded
                file.error = None
                counts["requeued"] += 1

        if marker is None:
            session.add(KeyValue(key=INDEPENDENT_MIGRATION_KEY, value=counts.copy()))
        else:
            marker.value = counts.copy()
        session.commit()
    return counts
