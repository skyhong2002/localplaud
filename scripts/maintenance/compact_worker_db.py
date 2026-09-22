#!/usr/bin/env python3
"""Build a compact, independently verified replacement candidate for a worker DB.

This tool is intentionally offline-only.  It never swaps, renames, truncates, or
deletes the source database. Its source write lock preserves logical content;
SQLite may checkpoint/remove WAL sidecars when the last connection closes.  The operator remains responsible for validating and
installing the resulting candidate while the worker is stopped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import struct
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MIN_FREE_BYTES = 10 * 1024**3
# A row is decoded in memory, so reject exceptionally large records safely.
MAX_SQLITE_VALUE_BYTES = 512 * 1024**2
TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
ACTIVE_JOB_STATUSES = ("queued", "running")


class CompactionError(RuntimeError):
    """A safe, expected refusal or validation failure."""


@dataclass(frozen=True)
class Column:
    name: str
    declared_type: str
    primary_key_position: int
    hidden: int


@dataclass(frozen=True)
class Table:
    name: str
    sql: str | None
    columns: tuple[Column, ...]
    without_rowid: bool
    rowid_selector: str | None
    insert_rowid: bool

    @property
    def insert_columns(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns if column.hidden == 0)

    @property
    def selected_expressions(self) -> tuple[str, ...]:
        columns = tuple(quote_identifier(column.name) for column in self.columns)
        if self.rowid_selector is None:
            return columns
        return (quote_identifier(self.rowid_selector), *columns)

    @property
    def order_expressions(self) -> tuple[str, ...]:
        if self.rowid_selector is not None:
            return (quote_identifier(self.rowid_selector),)
        primary_key = sorted(
            (column for column in self.columns if column.primary_key_position),
            key=lambda column: column.primary_key_position,
        )
        return tuple(quote_identifier(column.name) for column in primary_key)


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def free_bytes(path: Path) -> int:
    """Return available bytes on the filesystem containing *path*."""
    return shutil.disk_usage(path).free


def _check_space(path: Path, minimum: int) -> None:
    available = free_bytes(path)
    if available < minimum:
        raise CompactionError(
            f"space guard failed: {available} bytes free, {minimum} bytes required"
        )


def _iter_rows(cursor: sqlite3.Cursor) -> Iterable[tuple[Any, ...]]:
    while True:
        row = cursor.fetchone()
        if row is None:
            return
        yield row


def _table_columns(connection: sqlite3.Connection, table_name: str) -> tuple[Column, ...]:
    cursor = connection.execute(f"PRAGMA table_xinfo({quote_identifier(table_name)})")
    columns = tuple(
        Column(
            name=str(row[1]),
            declared_type=str(row[2] or ""),
            primary_key_position=int(row[5]),
            hidden=int(row[6]),
        )
        for row in _iter_rows(cursor)
    )
    if not columns:
        raise CompactionError(f"table has no inspectable columns: {table_name}")
    return columns


def _rowid_plan(columns: tuple[Column, ...], without_rowid: bool) -> tuple[str | None, bool]:
    if without_rowid:
        if not any(column.primary_key_position for column in columns):
            raise CompactionError("WITHOUT ROWID table has no primary key")
        return None, False

    occupied = {column.name.casefold() for column in columns}
    for alias in ("rowid", "_rowid_", "oid"):
        if alias.casefold() not in occupied:
            return alias, True
    raise CompactionError("cannot preserve an inaccessible rowid")


def inspect_schema(connection: sqlite3.Connection) -> tuple[list[Table], list[str]]:
    """Inspect supported main-schema objects without reading table bodies."""
    table_modes: dict[str, str] = {}
    without_rowid_tables: set[str] = set()
    for row in _iter_rows(connection.execute("PRAGMA main.table_list")):
        table_modes[str(row[1])] = str(row[2])
        if row[4]:
            without_rowid_tables.add(str(row[1]))

    schema_rows = list(
        _iter_rows(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM main.sqlite_schema ORDER BY rowid"
            )
        )
    )
    virtual_tables = [
        str(row[1])
        for row in schema_rows
        if row[0] == "table"
        and row[3]
        and str(row[3]).lstrip().upper().startswith("CREATE VIRTUAL TABLE")
    ]
    if virtual_tables:
        raise CompactionError("unsupported virtual table(s): " + ", ".join(sorted(virtual_tables)))
    shadow_tables = sorted(name for name, kind in table_modes.items() if kind == "shadow")
    if shadow_tables:
        raise CompactionError(
            "unsupported virtual-table shadow table(s): " + ", ".join(shadow_tables)
        )
    unsupported_internal = sorted(
        str(row[1])
        for row in schema_rows
        if row[0] == "table" and str(row[1]).startswith("sqlite_") and row[1] != "sqlite_sequence"
    )
    if unsupported_internal:
        raise CompactionError(
            "unsupported SQLite internal table(s): " + ", ".join(unsupported_internal)
        )

    tables: list[Table] = []
    deferred_sql: list[str] = []
    for object_type, name_value, _owner, sql_value in schema_rows:
        name = str(name_value)
        sql = None if sql_value is None else str(sql_value)
        if object_type == "table":
            if name.startswith("sqlite_") and name != "sqlite_sequence":
                continue
            if name != "sqlite_sequence" and sql is None:
                raise CompactionError(f"table schema is unavailable: {name}")
            columns = _table_columns(connection, name)
            without_rowid = name in without_rowid_tables
            if name == "sqlite_sequence":
                without_rowid = False
            rowid_selector, insert_rowid = _rowid_plan(columns, without_rowid)
            tables.append(
                Table(
                    name=name,
                    sql=sql,
                    columns=columns,
                    without_rowid=without_rowid,
                    rowid_selector=rowid_selector,
                    insert_rowid=insert_rowid,
                )
            )
        elif object_type in {"index", "view", "trigger"} and sql is not None:
            deferred_sql.append(sql)
    return tables, deferred_sql


def _require_worker_contract(connection: sqlite3.Connection, tables: list[Table]) -> Table:
    remote_jobs = next((table for table in tables if table.name == "remote_jobs"), None)
    if remote_jobs is None:
        raise CompactionError("source does not contain the remote_jobs worker table")
    names = {column.name for column in remote_jobs.columns}
    missing = {"status", "input_manifest"} - names
    if missing:
        raise CompactionError("remote_jobs is missing required retention columns")
    active = connection.execute(
        "SELECT COUNT(*) FROM remote_jobs WHERE status IN (?, ?)", ACTIVE_JOB_STATUSES
    ).fetchone()[0]
    if active:
        raise CompactionError(f"worker is not offline: {active} queued/running job(s) remain")
    return remote_jobs


def _release_manifest(value: Any) -> str:
    try:
        manifest = json.loads(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CompactionError(
            "terminal remote_jobs contains malformed input_manifest JSON"
        ) from exc
    if not isinstance(manifest, dict):
        raise CompactionError("terminal remote_jobs input_manifest must be a JSON object")
    inputs = manifest.get("inputs", [])
    if not isinstance(inputs, list) or any(not isinstance(item, dict) for item in inputs):
        raise CompactionError(
            "terminal remote_jobs input_manifest inputs must be an array of objects"
        )
    manifest["inputs"] = [
        {key: item_value for key, item_value in item.items() if key != "value"} for item in inputs
    ]
    manifest["input_payloads_released"] = True
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))


def _transform_row(table: Table, row: tuple[Any, ...]) -> tuple[tuple[Any, ...], bool]:
    if table.name != "remote_jobs":
        return row, False
    offset = 1 if table.rowid_selector is not None else 0
    column_positions = {column.name: offset + index for index, column in enumerate(table.columns)}
    if row[column_positions["status"]] not in TERMINAL_JOB_STATUSES:
        return row, False
    values = list(row)
    values[column_positions["input_manifest"]] = _release_manifest(
        values[column_positions["input_manifest"]]
    )
    return tuple(values), True


def _hash_value(digest: Any, value: Any) -> None:
    if value is None:
        payload = b""
        kind = b"n"
    elif isinstance(value, bytes):
        payload = value
        kind = b"b"
    elif isinstance(value, str):
        payload = value.encode("utf-8", "surrogatepass")
        kind = b"s"
    elif isinstance(value, int):
        payload = str(value).encode("ascii")
        kind = b"i"
    elif isinstance(value, float):
        payload = struct.pack(">d", value)
        kind = b"f"
    else:  # sqlite3 should never produce another storage class.
        raise CompactionError(f"unsupported SQLite value type: {type(value).__name__}")
    digest.update(kind)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _hash_row(digest: Any, row: tuple[Any, ...]) -> None:
    digest.update(b"R")
    digest.update(len(row).to_bytes(4, "big"))
    for value in row:
        _hash_value(digest, value)


def _select_sql(table: Table) -> str:
    selected = ", ".join(table.selected_expressions)
    ordered = ", ".join(table.order_expressions)
    return f"SELECT {selected} FROM {quote_identifier(table.name)} ORDER BY {ordered}"


def _insert_sql(table: Table) -> str:
    columns = list(table.insert_columns)
    if table.insert_rowid:
        assert table.rowid_selector is not None
        columns.insert(0, table.rowid_selector)
    names = ", ".join(quote_identifier(column) for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    return f"INSERT INTO {quote_identifier(table.name)} ({names}) VALUES ({placeholders})"


def _insert_values(table: Table, selected_row: tuple[Any, ...]) -> tuple[Any, ...]:
    offset = 1 if table.rowid_selector is not None else 0
    values_by_name = {
        column.name: selected_row[offset + index] for index, column in enumerate(table.columns)
    }
    values = tuple(values_by_name[name] for name in table.insert_columns)
    if table.insert_rowid:
        return (selected_row[0], *values)
    return values


def _stream_expected_hash(connection: sqlite3.Connection, table: Table) -> tuple[int, str, int]:
    digest = hashlib.sha256()
    count = 0
    released = 0
    cursor = connection.execute(_select_sql(table))
    for row in _iter_rows(cursor):
        transformed, did_release = _transform_row(table, row)
        _hash_row(digest, transformed)
        count += 1
        released += int(did_release)
    return count, digest.hexdigest(), released


def _stream_actual_hash(connection: sqlite3.Connection, table: Table) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    cursor = connection.execute(_select_sql(table))
    for row in _iter_rows(cursor):
        _hash_row(digest, row)
        count += 1
    return count, digest.hexdigest()


def _copy_table(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    table: Table,
    guard_path: Path,
    minimum_free_bytes: int,
) -> tuple[int, str, int]:
    if table.name == "sqlite_sequence":
        destination.execute("DELETE FROM sqlite_sequence")
    digest = hashlib.sha256()
    count = 0
    released = 0
    select_cursor = source.execute(_select_sql(table))
    insert_sql = _insert_sql(table)
    for row in _iter_rows(select_cursor):
        _check_space(guard_path, minimum_free_bytes)
        transformed, did_release = _transform_row(table, row)
        # Reserve room for this row, its index entries and SQLite overhead.
        row_bytes = sum(
            len(value.encode("utf-8"))
            if isinstance(value, str)
            else len(value)
            if isinstance(value, bytes)
            else 16
            for value in transformed
        )
        _check_space(guard_path, minimum_free_bytes + row_bytes * 3)
        destination.execute(insert_sql, _insert_values(table, transformed))
        _hash_row(digest, transformed)
        count += 1
        released += int(did_release)
        if count % 100 == 0:
            print(f"copying {table.name}: {count} row(s)", file=sys.stderr, flush=True)
    return count, digest.hexdigest(), released


def _check_database_integrity(connection: sqlite3.Connection) -> None:
    foreign_key_problem = connection.execute("PRAGMA foreign_key_check").fetchone()
    if foreign_key_problem is not None:
        raise CompactionError("destination foreign_key_check failed")
    quick_check = connection.execute("PRAGMA quick_check").fetchone()
    if quick_check != ("ok",):
        raise CompactionError("destination quick_check failed")


def _schema_signature(connection: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return list(
        _iter_rows(
            connection.execute(
                """SELECT type, name, tbl_name, sql
                   FROM sqlite_schema
                   WHERE name = 'sqlite_sequence' OR name NOT LIKE 'sqlite_%'
                   ORDER BY type, name"""
            )
        )
    )


def verify_connections(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    tables: list[Table],
    expected: dict[str, tuple[int, str]] | None = None,
) -> dict[str, int]:
    """Verify every expected transformed row against an existing candidate."""
    if _schema_signature(source) != _schema_signature(destination):
        raise CompactionError("destination schema does not match source")
    counts: dict[str, int] = {}
    for table in tables:
        if expected is None:
            expected_count, expected_hash, _released = _stream_expected_hash(source, table)
        else:
            expected_count, expected_hash = expected[table.name]
        actual_count, actual_hash = _stream_actual_hash(destination, table)
        if actual_count != expected_count:
            raise CompactionError(f"row-count verification failed for table {table.name}")
        if actual_hash != expected_hash:
            raise CompactionError(f"streaming-hash verification failed for table {table.name}")
        counts[table.name] = actual_count
    _check_database_integrity(destination)
    return counts


def _open_source(source: Path) -> sqlite3.Connection:
    uri = source.resolve().as_uri() + "?mode=rw"
    connection = sqlite3.connect(uri, uri=True, timeout=0.0, isolation_level=None)
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_SQLITE_VALUE_BYTES)
    connection.execute("PRAGMA busy_timeout=0")
    return connection


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, isolation_level=None)


def verify_candidate(source: Path, output: Path) -> dict[str, int]:
    """Independently compare an existing candidate with its source."""
    with _open_source(source) as source_connection:
        source_connection.execute("BEGIN IMMEDIATE")
        try:
            tables, _deferred = inspect_schema(source_connection)
            _require_worker_contract(source_connection, tables)
            with _open_read_only(output) as destination:
                counts = verify_connections(source_connection, destination, tables)
                for pragma in ["page_size", "user_version", "application_id"]:
                    if (
                        source_connection.execute(f"PRAGMA {pragma}").fetchone()
                        != destination.execute(f"PRAGMA {pragma}").fetchone()
                    ):
                        raise CompactionError(f"destination {pragma} does not match source")
                if destination.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                    raise CompactionError("destination auto_vacuum is not INCREMENTAL")
                if destination.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
                    raise CompactionError("destination journal_mode is not DELETE")
                return counts
        finally:
            source_connection.rollback()


def _exclusive_create(path: Path, source_stat: os.stat_result) -> None:
    for candidate in _output_files(path):
        if os.path.lexists(candidate):
            raise CompactionError("output or an associated SQLite sidecar already exists")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        output_stat = os.fstat(descriptor)
        if (output_stat.st_dev, output_stat.st_ino) == (source_stat.st_dev, source_stat.st_ino):
            raise CompactionError("output collides with source")
    finally:
        os.close(descriptor)


def _output_files(path: Path) -> tuple[Path, ...]:
    return path, Path(f"{path}-journal"), Path(f"{path}-wal"), Path(f"{path}-shm")


def compact_database(
    source: Path,
    output: Path,
    *,
    acknowledge_offline: bool,
    space_guard_path: Path | None = None,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> dict[str, Any]:
    """Create and verify a compact worker database replacement candidate."""
    source = Path(source)
    output = Path(output)
    if not acknowledge_offline:
        raise CompactionError("--acknowledge-offline is required")
    if min_free_bytes < 0:
        raise CompactionError("--min-free-bytes must be non-negative")
    try:
        source_stat = source.stat()
    except FileNotFoundError as exc:
        raise CompactionError("source database does not exist") from exc
    if not source.is_file():
        raise CompactionError("source must be a regular SQLite file")
    if not output.parent.is_dir():
        raise CompactionError("output parent directory does not exist")
    for candidate in _output_files(output):
        if os.path.lexists(candidate):
            raise CompactionError("output or an associated SQLite sidecar already exists")
    source_namespace = {item.resolve() for item in _output_files(source)}
    if source_namespace.intersection(item.resolve() for item in _output_files(output)):
        raise CompactionError("output overlaps the source SQLite sidecar namespace")
    guard_path = Path(space_guard_path) if space_guard_path is not None else output.parent
    if not guard_path.exists():
        raise CompactionError("space guard path does not exist")

    _check_space(guard_path, min_free_bytes)
    created_output = False
    source_connection: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    try:
        source_connection = _open_source(source)
        try:
            source_connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise CompactionError(
                "cannot acquire source BEGIN IMMEDIATE lock; worker may be active"
            ) from exc

        tables, deferred_sql = inspect_schema(source_connection)
        _require_worker_contract(source_connection, tables)
        page_size = int(source_connection.execute("PRAGMA page_size").fetchone()[0])
        user_version = int(source_connection.execute("PRAGMA user_version").fetchone()[0])
        application_id = int(source_connection.execute("PRAGMA application_id").fetchone()[0])

        _exclusive_create(output, source_stat)
        created_output = True
        destination = sqlite3.connect(output, isolation_level=None)
        destination.execute("PRAGMA journal_mode=DELETE")
        destination.execute(f"PRAGMA page_size={page_size}")
        destination.execute("PRAGMA auto_vacuum=INCREMENTAL")
        destination.execute("PRAGMA foreign_keys=OFF")
        destination.execute("BEGIN IMMEDIATE")

        for table in tables:
            if table.name != "sqlite_sequence":
                assert table.sql is not None
                destination.execute(table.sql)

        copied_counts: dict[str, int] = {}
        copied_hashes: dict[str, str] = {}
        released_jobs = 0
        for table in tables:
            print(f"copying {table.name}", file=sys.stderr, flush=True)
            count, digest, released = _copy_table(
                source_connection,
                destination,
                table,
                guard_path,
                min_free_bytes,
            )
            copied_counts[table.name] = count
            copied_hashes[table.name] = digest
            released_jobs += released
            print(f"copied {table.name}: {count} row(s)", file=sys.stderr, flush=True)

        for sql in deferred_sql:
            _check_space(guard_path, min_free_bytes + output.stat().st_size * 2)
            destination.execute(sql)
        destination.execute(f"PRAGMA user_version={user_version}")
        destination.execute(f"PRAGMA application_id={application_id}")
        destination.commit()
        _check_space(guard_path, min_free_bytes)

        print("verifying row counts, streaming hashes, and SQLite integrity", file=sys.stderr)
        # The source write lock keeps this snapshot immutable. Hashes captured
        # while copying are the expected data; avoid rereading hundreds of GB
        # of discarded audio just to calculate those same hashes again.
        expected = {name: (copied_counts[name], digest) for name, digest in copied_hashes.items()}
        verified_counts = verify_connections(source_connection, destination, tables, expected)
        if verified_counts != copied_counts:
            raise CompactionError("post-copy table counts differ from copied counts")
        if destination.execute("PRAGMA page_size").fetchone()[0] != page_size:
            raise CompactionError("destination page_size does not match source")
        if str(destination.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "delete":
            raise CompactionError("destination journal_mode is not DELETE")
        if destination.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            raise CompactionError("destination auto_vacuum is not INCREMENTAL")
        if destination.execute("PRAGMA user_version").fetchone()[0] != user_version:
            raise CompactionError("destination user_version does not match source")
        if destination.execute("PRAGMA application_id").fetchone()[0] != application_id:
            raise CompactionError("destination application_id does not match source")

        return {
            "source_bytes": source_stat.st_size,
            "output_bytes": output.stat().st_size,
            "tables": len(tables),
            "rows": sum(copied_counts.values()),
            "released_terminal_jobs": released_jobs,
            "hashes_verified": len(tables),
            "foreign_key_check": "ok",
            "quick_check": "ok",
        }
    except (sqlite3.Error, OSError) as exc:
        if isinstance(exc, CompactionError):
            raise
        raise CompactionError(f"compaction failed: {exc}") from exc
    finally:
        if destination is not None:
            destination.close()
        if source_connection is not None:
            try:
                source_connection.rollback()
            finally:
                source_connection.close()
        if created_output and sys.exc_info()[0] is not None:
            for candidate in _output_files(output):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a compact offline worker SQLite replacement candidate."
    )
    parser.add_argument("--source", required=True, type=Path, help="existing worker SQLite file")
    parser.add_argument("--output", required=True, type=Path, help="new candidate file")
    parser.add_argument(
        "--acknowledge-offline",
        action="store_true",
        help="confirm the worker is stopped and the source is offline",
    )
    parser.add_argument(
        "--space-guard-path",
        type=Path,
        help="path on the filesystem whose free space is guarded (default: output parent)",
    )
    parser.add_argument(
        "--min-free-bytes",
        type=int,
        default=DEFAULT_MIN_FREE_BYTES,
        help=f"minimum guarded free space (default: {DEFAULT_MIN_FREE_BYTES})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = compact_database(
            arguments.source,
            arguments.output,
            acknowledge_offline=arguments.acknowledge_offline,
            space_guard_path=arguments.space_guard_path,
            min_free_bytes=arguments.min_free_bytes,
        )
    except CompactionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
