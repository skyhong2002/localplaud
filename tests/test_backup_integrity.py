"""Backup concurrency and restore drills use only tiny, isolated SQLite fixtures."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from localplaud import backups


def _workspace(monkeypatch, tmp_path, journal="WAL"):
    database = tmp_path / "source.db"
    media = tmp_path / "audio"
    original = media / "kept" / "audio.opus"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"fixture audio")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(f"PRAGMA journal_mode={journal}")
        connection.execute(
            "CREATE TABLE plaud_files (id TEXT PRIMARY KEY, audio_path TEXT, wav_path TEXT)"
        )
        connection.execute("INSERT INTO plaud_files VALUES ('kept', ?, NULL)", (str(original),))
        connection.commit()
    monkeypatch.setattr(
        backups,
        "get_settings",
        lambda: SimpleNamespace(
            store=SimpleNamespace(database_url=f"sqlite:///{database}"),
            poller=SimpleNamespace(download_dir=media),
        ),
    )
    return database, media, original


@pytest.mark.parametrize("journal", ["WAL", "DELETE"])
def test_snapshot_and_media_pins_block_publication_until_capture_finishes(
    monkeypatch, tmp_path, journal
):
    database, media, original = _workspace(monkeypatch, tmp_path, journal)
    locked = threading.Event()
    captured = threading.Event()
    committed = threading.Event()
    errors = []
    inventory = backups._media_files
    copy_media = backups._copy_media

    def upload():
        try:
            with closing(sqlite3.connect(database, timeout=0)) as connection:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    connection.execute("BEGIN IMMEDIATE")
            locked.set()
            assert captured.wait(3)
            late = media / "new-upload" / "audio.mp3"
            late.parent.mkdir()
            late.write_bytes(b"late upload")
            with closing(sqlite3.connect(database, timeout=2)) as connection:
                connection.execute(
                    "INSERT INTO plaud_files VALUES ('new-upload', ?, NULL)", (str(late),)
                )
                connection.commit()
            committed.set()
        except BaseException as exc:
            errors.append(exc)
            locked.set()
            committed.set()

    writer = threading.Thread(target=upload)

    def during_capture(root, snapshot):
        writer.start()
        assert locked.wait(3)
        assert not errors
        return inventory(root, snapshot)

    def after_capture(*args):
        captured.set()
        assert committed.wait(3)
        assert not errors
        return copy_media(*args)

    monkeypatch.setattr(backups, "_media_files", during_capture)
    monkeypatch.setattr(backups, "_copy_media", after_capture)
    try:
        result = backups.create_workspace_backup(include_media=True)
    finally:
        captured.set()
        writer.join(timeout=3)
    assert not writer.is_alive() and not errors
    assert result["media"]["files"] == 1
    with zipfile.ZipFile(backups.workspace_backup_path(result["name"])) as archive:
        restored = tmp_path / "snapshot.db"
        restored.write_bytes(archive.read("database/localplaud.db"))
        assert "media/new-upload/audio.mp3" not in archive.namelist()
        assert archive.read("media/kept/audio.opus") == original.read_bytes()
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute("SELECT id FROM plaud_files").fetchall() == [("kept",)]


def test_pinned_media_survives_eviction_and_restores_without_source_paths(monkeypatch, tmp_path):
    database, _media, original = _workspace(monkeypatch, tmp_path)
    payload = original.read_bytes()
    copy_media = backups._copy_media

    def evict_then_copy(*args):
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("UPDATE plaud_files SET audio_path=NULL")
            original.unlink()
            connection.commit()
        return copy_media(*args)

    monkeypatch.setattr(backups, "_copy_media", evict_then_copy)
    result = backups.create_workspace_backup(include_media=True)
    entry = result["media"]["entries"][0]
    assert entry["bytes"] == len(payload)
    assert entry["sha256"] == hashlib.sha256(payload).hexdigest()
    archive = backups.workspace_backup_path(result["name"])
    # A restore never consults the live settings or source paths.
    monkeypatch.setattr(backups, "get_settings", lambda: pytest.fail("restore read live settings"))
    restored = backups.restore_workspace_backup(
        archive, tmp_path / "restored", expected_sha256=result["sha256"]
    )
    with closing(sqlite3.connect(restored["database"])) as connection:
        path = Path(connection.execute("SELECT audio_path FROM plaud_files").fetchone()[0])
        assert path.is_relative_to(tmp_path / "restored")
        assert path.read_bytes() == payload
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT audio_path FROM plaud_files").fetchone() == (None,)


def test_in_place_media_change_aborts_without_publishing_or_partial_files(monkeypatch, tmp_path):
    _database, _media, original = _workspace(monkeypatch, tmp_path)
    copy_media = backups._copy_media

    def change_then_copy(*args):
        original.write_bytes(b"modified after snapshot, before copy")
        return copy_media(*args)

    monkeypatch.setattr(backups, "_copy_media", change_then_copy)
    with pytest.raises(RuntimeError, match="media changed"):
        backups.create_workspace_backup(include_media=True)
    assert list(backups.backup_root().iterdir()) == []


@pytest.mark.parametrize("missing", [True, False])
def test_full_backup_rejects_missing_or_symlinked_snapshot_media(monkeypatch, tmp_path, missing):
    _database, _media, original = _workspace(monkeypatch, tmp_path)
    original.unlink()
    if not missing:
        outside = tmp_path / "outside-fixture"
        outside.write_bytes(b"never include this fixture")
        original.symlink_to(outside)
    with pytest.raises(OSError):
        backups.create_workspace_backup(include_media=True)
    assert list(backups.backup_root().iterdir()) == []
    # An intentionally database-only archive remains available.
    assert backups.create_workspace_backup()["media"]["included"] is False


def test_ancestor_symlink_swap_cannot_escape_media_root(monkeypatch, tmp_path):
    _database, media, original = _workspace(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "audio.opus").write_bytes(b"outside fixture")
    open_media = backups._open_media

    def swap_then_open(root, relative):
        original.parent.rename(media / "moved")
        original.parent.symlink_to(outside, target_is_directory=True)
        return open_media(root, relative)

    monkeypatch.setattr(backups, "_open_media", swap_then_open)
    with pytest.raises(OSError):
        backups.create_workspace_backup(include_media=True)
    assert list(backups.backup_root().iterdir()) == []


@pytest.mark.parametrize("corruption", ["media", "database", "traversal", "reference", "source_paths"])
def test_restore_rejects_corrupt_or_unsafe_members_and_cleans_up(monkeypatch, tmp_path, corruption):
    _workspace(monkeypatch, tmp_path)
    result = backups.create_workspace_backup(include_media=True)
    source = backups.workspace_backup_path(result["name"])
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(source) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(contents["manifest.json"])
    if corruption in {"media", "database"}:
        name = "media/kept/audio.opus" if corruption == "media" else "database/localplaud.db"
        contents[name] = b"x" * len(contents[name])
    elif corruption == "traversal":
        entry = manifest["media"]["entries"][0]
        contents["media/../../escaped"] = contents.pop(entry["archive_path"])
        entry["archive_path"] = "media/../../escaped"
    elif corruption == "reference":
        manifest["media"]["references"] = []
    else:
        del manifest["media"]["source_paths"]
    contents["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(bad, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    destination = tmp_path / "refused"
    with pytest.raises(ValueError):
        backups.restore_workspace_backup(bad, destination, expected_sha256=backups.file_sha256(bad))
    assert not destination.exists()
    assert not (tmp_path / "escaped").exists()


def test_restore_requires_trusted_digest_and_new_destination(monkeypatch, tmp_path):
    _workspace(monkeypatch, tmp_path)
    result = backups.create_workspace_backup(include_media=True)
    source = backups.workspace_backup_path(result["name"])
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "owned.txt"
    marker.write_text("keep")
    with pytest.raises(ValueError, match="checksum"):
        backups.restore_workspace_backup(source, tmp_path / "new", expected_sha256="0" * 64)
    with pytest.raises(FileExistsError):
        backups.restore_workspace_backup(source, destination, expected_sha256=result["sha256"])
    assert marker.read_text() == "keep"


def test_restore_rejects_swapped_recording_media_mappings(monkeypatch, tmp_path):
    database, media, _original = _workspace(monkeypatch, tmp_path)
    other = media / "other" / "audio.opus"
    other.parent.mkdir()
    other.write_bytes(b"other recording fixture")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("INSERT INTO plaud_files VALUES ('other', ?, NULL)", (str(other),))
        connection.commit()
    result = backups.create_workspace_backup(include_media=True)
    source = backups.workspace_backup_path(result["name"])
    with zipfile.ZipFile(source) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(contents["manifest.json"])
    first, second = manifest["media"]["references"]
    first["archive_path"], second["archive_path"] = second["archive_path"], first["archive_path"]
    contents["manifest.json"] = json.dumps(manifest).encode()
    bad = tmp_path / "swapped.zip"
    with zipfile.ZipFile(bad, "w") as archive:
        for name, data in contents.items():
            archive.writestr(name, data)
    destination = tmp_path / "refused-swapped"
    with pytest.raises(ValueError, match="media references mismatch"):
        backups.restore_workspace_backup(bad, destination, expected_sha256=backups.file_sha256(bad))
    assert not destination.exists()


@pytest.mark.parametrize("source_path", ["audio/kept/audio.opus", "~/audio/kept/audio.opus"])
def test_restore_maps_relative_and_home_paths_without_live_source_context(
    monkeypatch, tmp_path, source_path
):
    database, _media, original = _workspace(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE plaud_files SET audio_path=?", (source_path,))
        connection.commit()
    result = backups.create_workspace_backup(include_media=True)
    source = backups.workspace_backup_path(result["name"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("HOME", str(elsewhere))
    monkeypatch.setattr(backups, "get_settings", lambda: pytest.fail("restore loaded settings"))
    monkeypatch.setattr(Path, "home", lambda: pytest.fail("restore consulted live home"))
    restored = backups.restore_workspace_backup(
        source, tmp_path / "isolated", expected_sha256=result["sha256"]
    )
    with closing(sqlite3.connect(restored["database"])) as connection:
        path = Path(connection.execute("SELECT audio_path FROM plaud_files").fetchone()[0])
    assert path == tmp_path / "isolated/media/kept/audio.opus"
    assert path.read_bytes() == original.read_bytes()


@pytest.mark.parametrize("change", ["replace", "inplace"])
def test_restore_never_accepts_another_valid_zip_after_trusted_hash(monkeypatch, tmp_path, change):
    _database, _media, original = _workspace(monkeypatch, tmp_path)
    first = backups.create_workspace_backup(include_media=True)
    source = backups.workspace_backup_path(first["name"])
    original.write_bytes(b"different recording fixture")
    second = backups.create_workspace_backup(include_media=True)
    replacement = backups.workspace_backup_path(second["name"])
    stream_sha256 = backups._stream_sha256
    hashed_fd = None

    def hash_then_change(handle):
        nonlocal hashed_fd
        digest = stream_sha256(handle)
        if hashed_fd is None:
            hashed_fd = handle.fileno()
            if change == "replace":
                os.replace(replacement, source)
            else:
                with source.open("r+b") as output:
                    output.write(replacement.read_bytes())
                    output.truncate()
        else:
            assert handle.fileno() == hashed_fd
        return digest

    monkeypatch.setattr(backups, "_stream_sha256", hash_then_change)
    destination = tmp_path / "refused-race"
    with pytest.raises(ValueError, match="archive changed during restore"):
        backups.restore_workspace_backup(source, destination, expected_sha256=first["sha256"])
    assert not destination.exists()


@pytest.mark.parametrize("restore_bytes", [False, True])
def test_restore_rejects_same_size_inplace_writes_even_with_restored_mtime(
    monkeypatch, tmp_path, restore_bytes
):
    _workspace(monkeypatch, tmp_path)
    result = backups.create_workspace_backup(include_media=True)
    source = backups.workspace_backup_path(result["name"])
    with zipfile.ZipFile(source, "a") as archive:
        archive.comment = b"A"
    trusted = backups.file_sha256(source)
    before = source.stat()
    read_manifest = backups._read_manifest

    def read_then_modify(handle):
        manifest = read_manifest(handle)
        # ZIP comments are outside member checksums. Both variants are valid ZIPs.
        with source.open("r+b") as output:
            output.seek(-1, os.SEEK_END)
            output.write(b"B")
            output.flush()
            if restore_bytes:
                output.seek(-1, os.SEEK_END)
                output.write(b"A")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert source.stat().st_size == before.st_size
        return manifest

    monkeypatch.setattr(backups, "_read_manifest", read_then_modify)
    destination = tmp_path / "refused-inplace"
    with pytest.raises(ValueError, match="archive changed during restore"):
        backups.restore_workspace_backup(source, destination, expected_sha256=trusted)
    assert not destination.exists()


def test_unreferenced_uploads_and_staging_files_are_excluded_and_archives_are_private(
    monkeypatch, tmp_path
):
    _database, media, original = _workspace(monkeypatch, tmp_path)
    late = media / "uncommitted-upload"
    late.mkdir()
    (late / "audio.wav").write_bytes(b"not in the database")
    (original.parent / ".audio.tmp.wav").write_bytes(b"unfinished conversion")
    result = backups.create_workspace_backup(include_media=True)
    archive = backups.workspace_backup_path(result["name"])
    assert archive.stat().st_mode & 0o077 == 0
    assert archive.with_suffix(".zip.sha256").stat().st_mode & 0o077 == 0
    assert result["media"]["files"] == 1
    assert result["media"]["bytes"] == original.stat().st_size
    assert not list(backups.backup_root().glob("*.partial"))
    restored = backups.restore_workspace_backup(
        archive, tmp_path / "restored-private", expected_sha256=result["sha256"]
    )
    for path in (
        Path(restored["database"]),
        Path(restored["media_root"]) / "kept/audio.opus",
        tmp_path / "restored-private/manifest.json",
    ):
        assert path.stat().st_mode & 0o077 == 0
