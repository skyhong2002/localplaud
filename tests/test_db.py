"""DB round-trip against a temporary SQLite file: init, write, read back."""

import sqlite3

from sqlalchemy import text

import localplaud.config as config
import localplaud.db.session as db_session
from localplaud.db.models import FileStatus, PlaudFile, Transcript
from localplaud.db.session import init_db, session_scope


def _fresh_db(monkeypatch, tmp_path):
    """Point settings at a tmp sqlite file and reset the engine singletons."""
    db_file = tmp_path / "localplaud-test.db"
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.chdir(tmp_path)  # avoid picking up a real config.toml/.env
    config.get_settings(reload=True)
    # The engine is a module-level singleton created lazily from settings;
    # reset it so it binds to the tmp database. monkeypatch restores the
    # previous engine/sessionmaker at teardown.
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    return db_file


def test_init_and_roundtrip_with_relationship(monkeypatch, tmp_path):
    db_file = _fresh_db(monkeypatch, tmp_path)
    init_db()
    assert db_file.exists()

    with session_scope() as session:
        f = PlaudFile(
            id="dab5c6ca728964152f32d93ed76c1950",
            filename="2026-07-09 15:38:57",
            fullname="dab5c6ca728964152f32d93ed76c1950.opus",
            filesize=9958560,
            file_md5="0d1a2f87",
            duration_ms=2489000,
            version=1783594217,
            cloud_is_trans=False,
            raw={"edit_from": "android"},
        )
        f.transcript = Transcript(
            provider="dummy",
            model="test-model",
            language="en",
            text="hello world",
            segments=[{"text": "hello world", "start": 0.0, "end": 1.0, "speaker": None}],
        )
        session.add(f)
        # session_scope commits on exit

    with session_scope() as session:
        got = session.get(PlaudFile, "dab5c6ca728964152f32d93ed76c1950")
        assert got is not None
        assert got.filename == "2026-07-09 15:38:57"
        assert got.status == FileStatus.discovered  # default local state
        assert got.raw == {"edit_from": "android"}

        # Relationship survived the round-trip, both directions.
        assert got.transcript is not None
        assert got.transcript.text == "hello world"
        assert got.transcript.provider == "dummy"
        assert got.transcript.segments[0]["text"] == "hello world"
        assert got.transcript.file is got


def test_sqlite_connections_use_wal_and_a_long_busy_timeout(monkeypatch, tmp_path):
    """Concurrent API/poller/worker writes must wait, not fail the stage.

    Without these the driver's 5s default turns ordinary contention into
    ``database is locked``, which the pipeline records as a failed stage run.
    """
    _fresh_db(monkeypatch, tmp_path)
    init_db()

    with session_scope() as session:
        assert session.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert session.execute(text("PRAGMA busy_timeout")).scalar() == 30000
        assert session.execute(text("PRAGMA synchronous")).scalar() == 1  # NORMAL


def test_an_open_write_transaction_does_not_block_readers(monkeypatch, tmp_path):
    """WAL's actual guarantee: the web UI keeps reading while a stage writes."""
    db_file = _fresh_db(monkeypatch, tmp_path)
    init_db()

    with session_scope() as session:
        session.add(PlaudFile(id="already-there", filename="x"))

    writer = sqlite3.connect(db_file, timeout=30)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE plaud_files SET filename='uncommitted' WHERE id='already-there'")
    try:
        # Under the default rollback journal this read raises "database is
        # locked"; under WAL it sees the last committed snapshot.
        with session_scope() as session:
            assert session.get(PlaudFile, "already-there").filename == "x"
    finally:
        writer.rollback()
        writer.close()


def test_session_scope_rolls_back_on_error(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    init_db()

    try:
        with session_scope() as session:
            session.add(PlaudFile(id="rollback-me", filename="x"))
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    with session_scope() as session:
        assert session.get(PlaudFile, "rollback-me") is None
