from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "maintenance" / "compact_worker_db.py"
SPEC = importlib.util.spec_from_file_location("compact_worker_db", SCRIPT)
assert SPEC and SPEC.loader
compact_worker_db = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compact_worker_db
SPEC.loader.exec_module(compact_worker_db)


def _manifest(label: str, *, nested: bool = False) -> dict:
    value = {"segments": [{"text": label}]} if nested else f"payload-{label}"
    return {
        "stage": "transcribe",
        "inputs": [
            {
                "name": "audio",
                "kind": "inline_base64",
                "value": value,
                "sha256": f"checksum-{label}",
                "metadata": {"value": "nested metadata remains"},
            },
            {"name": "settings", "value": None, "media_type": "application/json"},
        ],
        "value": "top-level value remains",
        "request_metadata": {"owner": label},
    }


def _create_worker_db(path: Path, jobs: list[tuple[str, str, object]] | None = None) -> None:
    jobs = jobs or [
        ("success", "succeeded", _manifest("success", nested=True)),
        ("failure", "failed", _manifest("failure")),
        ("cancel", "cancelled", _manifest("cancel")),
        ("other", "expired", _manifest("other")),
    ]
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA page_size=8192")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA user_version=47")
    connection.execute("PRAGMA application_id=1280330575")
    connection.executescript(
        """
        CREATE TABLE remote_jobs (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            protocol_version TEXT NOT NULL DEFAULT '1',
            stage TEXT NOT NULL,
            model TEXT,
            status TEXT NOT NULL,
            progress REAL NOT NULL DEFAULT 0,
            input_manifest JSON NOT NULL,
            options JSON NOT NULL,
            artifacts JSON NOT NULL,
            error JSON,
            cancel_requested BOOLEAN NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES remote_jobs(id),
            body BLOB,
            checksum TEXT,
            note TEXT
        );
        CREATE TABLE rowid_data (label TEXT, payload BLOB, nullable TEXT);
        CREATE TABLE composite_data (
            left_id TEXT,
            right_id INTEGER,
            body BLOB,
            PRIMARY KEY (left_id, right_id)
        ) WITHOUT ROWID;
        CREATE TABLE audit_log (message TEXT);
        CREATE INDEX ix_artifacts_checksum ON artifacts(checksum);
        CREATE VIEW terminal_jobs AS
            SELECT id, status FROM remote_jobs
            WHERE status IN ('succeeded', 'failed', 'cancelled');
        CREATE TRIGGER artifacts_audit AFTER INSERT ON artifacts BEGIN
            INSERT INTO audit_log(message) VALUES ('artifact inserted');
        END;
        """
    )
    for job_id, status, manifest in jobs:
        serialized = manifest if isinstance(manifest, str) else json.dumps(manifest)
        connection.execute(
            """INSERT INTO remote_jobs(
                id, idempotency_key, stage, model, status, progress, input_manifest,
                options, artifacts, error, attempts, created_at, completed_at, updated_at
            ) VALUES (?, ?, 'transcribe', 'large-v3-turbo', ?, 1, ?, ?, ?, ?, 3, ?, ?, ?)""",
            (
                job_id,
                f"idem-{job_id}",
                status,
                serialized,
                json.dumps({"language": "zh", "keep": job_id}),
                json.dumps([{"name": "result.json", "data_base64": "c2VjcmV0"}]),
                json.dumps({"code": "kept", "message": f"error-{job_id}"}),
                "2026-09-01T00:00:00Z",
                "2026-09-01T00:01:00Z",
                "2026-09-01T00:01:00Z",
            ),
        )
    connection.execute(
        "INSERT INTO artifacts(id, job_id, body, checksum, note) VALUES (17, ?, ?, ?, NULL)",
        (jobs[0][0], sqlite3.Binary(b"\x00private\xffartifact"), "artifact-checksum"),
    )
    connection.execute(
        "INSERT INTO rowid_data(rowid, label, payload, nullable) VALUES (42, ?, ?, NULL)",
        ("ordinary", sqlite3.Binary(b"\x00\x01\xfe\xff")),
    )
    connection.execute(
        "INSERT INTO composite_data VALUES ('pair', 9, ?)",
        (sqlite3.Binary(b"composite"),),
    )
    connection.execute("DELETE FROM audit_log")
    connection.commit()
    # Leave committed content in a WAL to exercise source reading without a checkpoint.
    connection.execute("INSERT INTO audit_log(rowid, message) VALUES (77, 'wal-visible')")
    connection.commit()
    connection.close()


def _compact(source: Path, output: Path, **kwargs):
    return compact_worker_db.compact_database(
        source,
        output,
        acknowledge_offline=True,
        min_free_bytes=0,
        **kwargs,
    )


def _object_sql(connection: sqlite3.Connection, object_type: str) -> dict[str, str]:
    return dict(
        connection.execute(
            "SELECT name, sql FROM sqlite_schema WHERE type=? AND sql IS NOT NULL",
            (object_type,),
        )
    )


def test_compacts_worker_db_with_exact_retention_and_schema_preservation(tmp_path):
    source = tmp_path / "worker.db"
    output = tmp_path / "worker.compact.db"
    _create_worker_db(source)

    result = _compact(source, output)

    assert output.exists()
    assert result["released_terminal_jobs"] == 3
    assert result["hashes_verified"] == result["tables"]
    assert result["foreign_key_check"] == result["quick_check"] == "ok"
    with sqlite3.connect(source) as original, sqlite3.connect(output) as compacted:
        original_jobs = {
            row[0]: row for row in original.execute("SELECT * FROM remote_jobs ORDER BY id")
        }
        compacted_jobs = {
            row[0]: row for row in compacted.execute("SELECT * FROM remote_jobs ORDER BY id")
        }
        columns = [row[1] for row in compacted.execute("PRAGMA table_info(remote_jobs)")]
        manifest_index = columns.index("input_manifest")
        for job_id in ("success", "failure", "cancel"):
            before = original_jobs[job_id]
            after = compacted_jobs[job_id]
            assert before[:manifest_index] + before[manifest_index + 1 :] == (
                after[:manifest_index] + after[manifest_index + 1 :]
            )
            source_manifest = json.loads(before[manifest_index])
            output_manifest = json.loads(after[manifest_index])
            assert output_manifest["input_payloads_released"] is True
            assert all("value" not in item for item in output_manifest["inputs"])
            assert output_manifest["inputs"][0]["sha256"] == source_manifest["inputs"][0]["sha256"]
            assert output_manifest["inputs"][0]["metadata"] == {"value": "nested metadata remains"}
            assert output_manifest["value"] == "top-level value remains"
        assert compacted_jobs["other"] == original_jobs["other"]

        assert compacted.execute(
            "SELECT id, job_id, body, checksum, note FROM artifacts"
        ).fetchone() == (17, "success", b"\x00private\xffartifact", "artifact-checksum", None)
        assert compacted.execute(
            "SELECT rowid, label, payload, nullable FROM rowid_data"
        ).fetchone() == (42, "ordinary", b"\x00\x01\xfe\xff", None)
        assert compacted.execute("SELECT rowid, message FROM audit_log").fetchall() == [
            (77, "wal-visible")
        ]
        assert compacted.execute("SELECT * FROM composite_data").fetchone() == (
            "pair",
            9,
            b"composite",
        )
        assert compacted.execute("SELECT name, seq FROM sqlite_sequence").fetchone() == (
            "artifacts",
            17,
        )
        assert _object_sql(compacted, "index") == _object_sql(original, "index")
        assert _object_sql(compacted, "view") == _object_sql(original, "view")
        assert _object_sql(compacted, "trigger") == _object_sql(original, "trigger")
        assert compacted.execute("SELECT COUNT(*) FROM terminal_jobs").fetchone() == (3,)
        assert compacted.execute("PRAGMA user_version").fetchone() == (47,)
        assert compacted.execute("PRAGMA application_id").fetchone() == (1280330575,)
        assert (
            compacted.execute("PRAGMA page_size").fetchone()
            == original.execute("PRAGMA page_size").fetchone()
        )
        assert compacted.execute("PRAGMA auto_vacuum").fetchone() == (2,)
        assert compacted.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"

        # Triggers are preserved but did not fire while historical rows were copied.
        compacted.execute(
            "INSERT INTO artifacts(job_id, body, checksum) VALUES ('success', X'01', 'new')"
        )
        assert compacted.execute("SELECT message FROM audit_log WHERE rowid != 77").fetchone() == (
            "artifact inserted",
        )


def test_actual_worker_schema_matches_release_job_inputs_contract(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from localplaud.db.models import Base, RemoteJob
    from localplaud.remote.server import release_job_inputs

    source = tmp_path / "actual-worker.db"
    output = tmp_path / "actual-worker.compact.db"
    engine = create_engine(f"sqlite:///{source}")
    Base.metadata.create_all(engine)
    expected = {}
    with Session(engine) as session:
        for status in ("succeeded", "failed", "cancelled"):
            manifest = _manifest(status, nested=True)
            reference = RemoteJob(
                id=f"reference-{status}",
                idempotency_key=f"reference-{status}",
                stage="transcribe",
                status=status,
                input_manifest=manifest,
            )
            release_job_inputs(reference)
            expected[status] = reference.input_manifest
            session.add(
                RemoteJob(
                    id=status,
                    idempotency_key=f"idem-{status}",
                    stage="transcribe",
                    status=status,
                    input_manifest=manifest,
                    artifacts=[{"name": "result", "data_base64": "cHJlc2VydmU="}],
                    error={"code": "preserve", "retryable": False},
                    options={"language": "zh"},
                )
            )
        session.commit()
    engine.dispose()

    result = _compact(source, output)

    assert result["released_terminal_jobs"] == 3
    with sqlite3.connect(output) as connection:
        actual = {
            status: json.loads(manifest)
            for status, manifest in connection.execute(
                "SELECT status, input_manifest FROM remote_jobs ORDER BY status"
            )
        }
    assert actual == expected


@pytest.mark.parametrize("status", ["queued", "running"])
def test_refuses_active_worker_jobs_without_creating_output(tmp_path, status):
    source = tmp_path / "worker.db"
    output = tmp_path / "candidate.db"
    manifest = _manifest("active")
    _create_worker_db(source, [("active", status, manifest)])

    with pytest.raises(compact_worker_db.CompactionError, match="worker is not offline"):
        _compact(source, output)

    assert not output.exists()
    with sqlite3.connect(source) as connection:
        assert (
            json.loads(connection.execute("SELECT input_manifest FROM remote_jobs").fetchone()[0])
            == manifest
        )


def test_malformed_terminal_manifest_aborts_and_removes_only_new_output(tmp_path):
    source = tmp_path / "worker.db"
    output = tmp_path / "candidate.db"
    _create_worker_db(source, [("broken", "failed", "{not-json")])

    with pytest.raises(compact_worker_db.CompactionError, match="malformed input_manifest"):
        _compact(source, output)

    assert source.exists()
    assert not output.exists()
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT input_manifest FROM remote_jobs").fetchone() == (
            "{not-json",
        )


def test_rejects_preexisting_output_symlink_and_hardlink_without_touching_them(tmp_path):
    source = tmp_path / "worker.db"
    _create_worker_db(source)
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"operator-owned")
    symlink = tmp_path / "candidate-link.db"
    symlink.symlink_to(existing)
    hardlink = tmp_path / "candidate-hardlink.db"
    os.link(source, hardlink)

    for target in (existing, symlink, hardlink):
        with pytest.raises(
            compact_worker_db.CompactionError,
            match="output or an associated SQLite sidecar already exists",
        ):
            _compact(source, target)

    assert existing.read_bytes() == b"operator-owned"
    assert symlink.is_symlink()
    assert os.stat(hardlink).st_ino == os.stat(source).st_ino


def test_keeps_source_begin_immediate_lock_through_streaming_copy(monkeypatch, tmp_path):
    source = tmp_path / "worker.db"
    output = tmp_path / "candidate.db"
    _create_worker_db(source)
    calls = 0
    lock_was_held = False

    def inspect_lock(_path):
        nonlocal calls, lock_was_held
        calls += 1
        if calls == 2:
            contender = sqlite3.connect(source, timeout=0, isolation_level=None)
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    contender.execute("BEGIN IMMEDIATE")
                lock_was_held = True
            finally:
                contender.close()
        return 1024**3

    monkeypatch.setattr(compact_worker_db, "free_bytes", inspect_lock)
    compact_worker_db.compact_database(
        source,
        output,
        acknowledge_offline=True,
        min_free_bytes=1,
    )

    assert lock_was_held


def test_refuses_virtual_tables_instead_of_silently_dropping_them(tmp_path):
    source = tmp_path / "worker.db"
    output = tmp_path / "candidate.db"
    _create_worker_db(source)
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE VIRTUAL TABLE searchable USING fts5(body)")

    with pytest.raises(compact_worker_db.CompactionError, match="unsupported virtual table"):
        _compact(source, output)

    assert not output.exists()


def test_low_space_after_exclusive_creation_cleans_candidate_and_preserves_source(
    monkeypatch, tmp_path
):
    source = tmp_path / "worker.db"
    output = tmp_path / "candidate.db"
    _create_worker_db(source)
    before = source.read_bytes()
    checks = iter([100, 100, 0])
    monkeypatch.setattr(compact_worker_db, "free_bytes", lambda _path: next(checks, 0))

    with pytest.raises(compact_worker_db.CompactionError, match="space guard failed"):
        compact_worker_db.compact_database(
            source,
            output,
            acknowledge_offline=True,
            min_free_bytes=1,
        )

    assert not output.exists()
    assert source.read_bytes() == before
    with sqlite3.connect(source) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_independent_streaming_verifier_detects_candidate_tampering(tmp_path):
    source = tmp_path / "worker.db"
    output = tmp_path / "candidate.db"
    _create_worker_db(source)
    _compact(source, output)
    with sqlite3.connect(output) as connection:
        connection.execute("UPDATE artifacts SET body=X'00' WHERE id=17")
        connection.commit()

    with pytest.raises(
        compact_worker_db.CompactionError, match="streaming-hash verification failed"
    ):
        compact_worker_db.verify_candidate(source, output)


def test_cli_requires_explicit_offline_acknowledgement(tmp_path):
    source = tmp_path / "worker.db"
    output = tmp_path / "candidate.db"
    _create_worker_db(source)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source",
            str(source),
            "--output",
            str(output),
            "--min-free-bytes",
            "0",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "--acknowledge-offline is required" in result.stderr
    assert not output.exists()


def test_preserves_rowids_for_composite_and_descending_integer_primary_keys(tmp_path):
    source = tmp_path / "worker.db"
    output = tmp_path / "worker.compact.db"
    _create_worker_db(source)
    with sqlite3.connect(source) as connection:
        connection.executescript("""
            CREATE TABLE compound (a INTEGER, b TEXT, PRIMARY KEY(a,b));
            INSERT INTO compound(rowid,a,b) VALUES (101,3,'one'),(102,3,'two');
            CREATE TABLE descending (a INTEGER PRIMARY KEY DESC, b TEXT);
            INSERT INTO descending(rowid,a,b) VALUES (203,7,'kept');
            CREATE TABLE text_keys (a TEXT PRIMARY KEY, b TEXT) WITHOUT ROWID;
            INSERT INTO text_keys VALUES ('a','one'),('b','two');
        """)
    _compact(source, output)
    with sqlite3.connect(source) as original, sqlite3.connect(output) as compacted:
        for table in ["compound", "descending"]:
            query = f"SELECT rowid,* FROM {table} ORDER BY rowid"
            assert original.execute(query).fetchall() == compacted.execute(query).fetchall()
        assert compacted.execute("SELECT * FROM text_keys ORDER BY a").fetchall() == [
            ("a", "one"),
            ("b", "two"),
        ]


def test_refuses_source_sidecar_output_namespace(tmp_path):
    source = tmp_path / "worker.db"
    _create_worker_db(source)
    with pytest.raises(compact_worker_db.CompactionError, match="sidecar namespace"):
        _compact(source, Path(str(source) + "-journal"))
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT COUNT(*) FROM remote_jobs").fetchone()[0] == 4


def test_independent_verification_rejects_changed_database_metadata(tmp_path):
    source = tmp_path / "worker.db"
    output = tmp_path / "worker.compact.db"
    _create_worker_db(source)
    _compact(source, output)
    with sqlite3.connect(output) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(compact_worker_db.CompactionError, match="user_version"):
        compact_worker_db.verify_candidate(source, output)
