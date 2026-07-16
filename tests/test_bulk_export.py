"""Focused tests for deterministic bulk recording archives."""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from io import BytesIO
from pathlib import PurePosixPath

import pytest
from docx import Document
from pypdf import PdfReader
from sqlalchemy import event

import localplaud.bulk_export as bulk_export
import localplaud.config as config
import localplaud.db.session as db_session
import localplaud.export_formats as export_formats
from localplaud.bulk_export import (
    BulkExportRequest,
    BulkExportValidationError,
    NoExportableContentError,
    UnknownRecordingIdsError,
    build_bulk_export,
)
from localplaud.db.models import PlaudFile, StageName, StageRun, Summary, Transcript, UserNote
from localplaud.db.session import get_engine, init_db, session_scope

FIRST_ID = "a" * 32
SECOND_ID = "b" * 32
NOTES_ONLY_ID = "c" * 32
BARE_ID = "d" * 32
STALE_ID = "e" * 32


def _segment(text: str, speaker: str = "SPEAKER_00") -> list[dict]:
    return [{"text": text, "start": 2.5, "end": 4.0, "speaker": speaker}]


@pytest.fixture
def seeded_db(monkeypatch, tmp_path):
    database = tmp_path / "bulk.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOCALPLAUD_STORE__DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("LOCALPLAUD_PIPELINE__ARTIFACT_MODE", "independent")
    config.get_settings(reload=True)
    monkeypatch.setattr(db_session, "_engine", None)
    monkeypatch.setattr(db_session, "_Session", None)
    init_db()

    with session_scope() as session:
        first = PlaudFile(id=FIRST_ID, filename="Planning Session")
        first.transcripts = [
            Transcript(
                provider="local-test",
                source="local",
                text="first transcript",
                segments=_segment("first transcript"),
            )
        ]
        first.summaries = [
            Summary(template="meeting", title="Decisions", content_md="- Ship milestone 10")
        ]

        second = PlaudFile(id=SECOND_ID, filename="Planning Session")
        second.transcripts = [
            Transcript(
                provider="local-test",
                source="local",
                text="second transcript",
                segments=_segment("second transcript", "SPEAKER_01"),
            )
        ]

        notes_only = PlaudFile(id=NOTES_ONLY_ID, filename="Notes only")
        notes_only.user_notes = [
            UserNote(title="Manual", content_md="A saved local note", source_type="manual")
        ]

        stale = PlaudFile(id=STALE_ID, filename="Independent source")
        stale.transcripts = [
            Transcript(
                provider="plaud",
                source="cloud",
                text="paid cloud transcript",
                segments=_segment("paid cloud transcript"),
            ),
            Transcript(
                provider="local-test",
                source="local",
                text="canonical local transcript",
                segments=_segment("canonical local transcript"),
            ),
        ]
        stale.summaries = [
            Summary(template="local", source="local", content_md="stale local summary"),
            Summary(template="plaud", source="cloud", content_md="paid cloud summary"),
        ]
        stale.stage_runs = [
            StageRun(stage=StageName.summarize, detail={"stale": True})
        ]

        session.add_all([first, second, notes_only, PlaudFile(id=BARE_ID, filename="Bare"), stale])
    return database


def _archive_bytes(result) -> bytes:
    try:
        return result.stream.read()
    finally:
        result.close()


def _read_archive(payload: bytes) -> tuple[list[str], dict, dict[str, bytes]]:
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        files = {name: archive.read(name) for name in names if name != "manifest.json"}
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
    return names, manifest, files


def _stem(recording_id: str, title: str) -> str:
    suffix = hashlib.sha256(recording_id.encode()).hexdigest()[:12]
    return f"{title}--{suffix}"


def test_exact_entries_manifest_and_deterministic_bytes(seeded_db):
    request = BulkExportRequest(
        recording_ids=[FIRST_ID], transcript_format="txt", notes_format="md"
    )
    first_payload = _archive_bytes(build_bulk_export(request))
    second_payload = _archive_bytes(build_bulk_export(request))
    assert first_payload == second_payload

    stem = _stem(FIRST_ID, "Planning-Session")
    names, manifest, files = _read_archive(first_payload)
    assert names == [
        f"recordings/{stem}/transcript.txt",
        f"recordings/{stem}/notes.md",
        "manifest.json",
    ]
    assert manifest["schema_version"] == 1
    assert manifest["requested_options"] == {
        "recording_ids": [FIRST_ID],
        "transcript_format": "txt",
        "notes_format": "md",
        "timestamps": True,
        "speakers": True,
    }
    assert manifest["recordings"][0]["id"] == FIRST_ID
    assert manifest["recordings"][0]["title"] == "Planning Session"
    assert manifest["recordings"][0]["transcript_lineage"] == {
        "transcript_id": 1,
        "transcript_source": "local",
        "transcript_revision_id": None,
        "transcript_revision": None,
    }
    outputs = manifest["recordings"][0]["outputs"]
    assert [output["status"] for output in outputs] == ["emitted", "emitted"]
    for output in outputs:
        assert output["size_bytes"] == len(files[output["path"]])
        assert output["sha256"] == hashlib.sha256(files[output["path"]]).hexdigest()


def test_transcript_options_are_passed_to_canonical_renderer(seeded_db):
    payload = _archive_bytes(
        build_bulk_export(
            BulkExportRequest(
                recording_ids=[FIRST_ID],
                transcript_format="txt",
                timestamps=False,
                speakers=False,
            )
        )
    )
    _names, manifest, files = _read_archive(payload)
    transcript = next(iter(files.values()))
    assert b"first transcript" in transcript
    assert b"[00:02]" not in transcript
    assert b"SPEAKER_00" not in transcript
    assert manifest["requested_options"]["timestamps"] is False
    assert manifest["requested_options"]["speakers"] is False


def test_manifest_and_outputs_render_from_one_recording_snapshot(seeded_db, monkeypatch):
    def snapshot_then_mutate(recording_id):
        snapshot = export_formats.recording_data(recording_id)
        with session_scope() as session:
            recording = session.get(PlaudFile, recording_id)
            recording.local_title = "Changed after snapshot"
            recording.local_transcript.text = "changed transcript"
            recording.local_transcript.segments = _segment("changed transcript")
        return snapshot

    monkeypatch.setattr(bulk_export, "recording_data", snapshot_then_mutate)
    payload = _archive_bytes(
        build_bulk_export(
            BulkExportRequest(
                recording_ids=[FIRST_ID], transcript_format="txt", notes_format="md"
            )
        )
    )
    names, manifest, files = _read_archive(payload)
    assert manifest["recordings"][0]["title"] == "Planning Session"
    assert "Planning-Session" in names[0]
    combined = b"\n".join(files.values())
    assert b"first transcript" in combined
    assert b"Changed after snapshot" not in combined
    assert b"changed transcript" not in combined


def test_pdf_archive_is_deterministic(seeded_db):
    request = BulkExportRequest(recording_ids=[FIRST_ID], transcript_format="pdf")
    first_payload = _archive_bytes(build_bulk_export(request))
    second_payload = _archive_bytes(build_bulk_export(request))
    assert first_payload == second_payload
    _names, _manifest, files = _read_archive(first_payload)
    pdf = next(iter(files.values()))
    assert pdf.startswith(b"%PDF-")
    assert len(PdfReader(BytesIO(pdf)).pages) == 1


def test_docx_archive_is_deterministic_and_valid_across_clock_ticks(seeded_db):
    request = BulkExportRequest(
        recording_ids=[FIRST_ID], transcript_format="docx", notes_format="docx"
    )
    first_payload = _archive_bytes(build_bulk_export(request))
    time.sleep(2.1)
    second_payload = _archive_bytes(build_bulk_export(request))
    assert first_payload == second_payload
    _names, _manifest, files = _read_archive(first_payload)
    assert len(files) == 2
    for payload in files.values():
        with zipfile.ZipFile(BytesIO(payload)) as document_archive:
            assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in document_archive.infolist())
        assert Document(BytesIO(payload)).paragraphs


def test_result_streams_in_bounded_chunks(seeded_db, monkeypatch):
    payload = hashlib.shake_256(b"bulk export chunk test").digest(2 * 1024 * 1024)
    monkeypatch.setattr(
        bulk_export,
        "render_transcript_data",
        lambda *_args, **_kwargs: (payload, "text/plain"),
    )
    result = build_bulk_export(
        BulkExportRequest(recording_ids=[FIRST_ID], transcript_format="txt")
    )
    try:
        chunks = list(result)
        assert len(chunks) >= 2
        assert all(len(chunk) <= 1024 * 1024 for chunk in chunks)
        assert sum(map(len, chunks)) == result.size_bytes
    finally:
        result.close()


def test_selection_deduplicates_and_preserves_first_seen_order(seeded_db):
    payload = _archive_bytes(
        build_bulk_export(
            BulkExportRequest(
                recording_ids=[SECOND_ID, FIRST_ID, SECOND_ID], transcript_format="vtt"
            )
        )
    )
    names, manifest, _files = _read_archive(payload)
    assert manifest["requested_options"]["recording_ids"] == [SECOND_ID, FIRST_ID]
    assert [row["id"] for row in manifest["recordings"]] == [SECOND_ID, FIRST_ID]
    assert names[:-1] == [
        f"recordings/{_stem(SECOND_ID, 'Planning-Session')}/transcript.vtt",
        f"recordings/{_stem(FIRST_ID, 'Planning-Session')}/transcript.vtt",
    ]


def test_unknown_ids_are_rejected_before_rendering(seeded_db, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("renderer must not run")

    monkeypatch.setattr(bulk_export, "render_transcript_data", forbidden)
    with pytest.raises(UnknownRecordingIdsError) as caught:
        build_bulk_export(
            BulkExportRequest(recording_ids=[FIRST_ID, "unknown", "also-unknown"], transcript_format="txt")
        )
    assert caught.value.recording_ids == ("unknown", "also-unknown")


@pytest.mark.parametrize(
    "bulk_request",
    [
        BulkExportRequest(recording_ids=[], transcript_format="txt"),
        BulkExportRequest(recording_ids=[FIRST_ID]),
        BulkExportRequest(recording_ids=[str(index) for index in range(51)], transcript_format="txt"),
        BulkExportRequest(recording_ids=[FIRST_ID], transcript_format="csv"),
        BulkExportRequest(recording_ids=[FIRST_ID], notes_format="html"),
    ],
)
def test_invalid_or_oversized_selections_are_rejected(seeded_db, bulk_request):
    with pytest.raises(BulkExportValidationError):
        build_bulk_export(bulk_request)


def test_unsafe_colliding_unicode_titles_get_safe_unique_bounded_paths(seeded_db):
    title = "../會議／紀錄\x00" + "長" * 200
    with session_scope() as session:
        session.get(PlaudFile, FIRST_ID).local_title = title
        session.get(PlaudFile, SECOND_ID).local_title = title

    payload = _archive_bytes(
        build_bulk_export(
            BulkExportRequest(recording_ids=[FIRST_ID, SECOND_ID], transcript_format="txt")
        )
    )
    names, _manifest, _files = _read_archive(payload)
    content_names = names[:-1]
    assert len(content_names) == len(set(content_names)) == 2
    for name in content_names:
        path = PurePosixPath(name)
        assert path.parts[0] == "recordings"
        assert ".." not in path.parts
        assert "\x00" not in name and "\\" not in name
        assert len(path.parts[1].encode("utf-8")) <= bulk_export.MAX_TITLE_BYTES + 14


def test_missing_content_is_partial_and_no_content_is_typed(seeded_db):
    payload = _archive_bytes(
        build_bulk_export(
            BulkExportRequest(
                recording_ids=[SECOND_ID, NOTES_ONLY_ID],
                transcript_format="txt",
                notes_format="md",
            )
        )
    )
    names, manifest, _files = _read_archive(payload)
    assert len(names) == 3
    statuses = [
        [output["status"] for output in recording["outputs"]]
        for recording in manifest["recordings"]
    ]
    assert statuses == [["emitted", "skipped"], ["skipped", "emitted"]]

    with pytest.raises(NoExportableContentError) as caught:
        build_bulk_export(
            BulkExportRequest(
                recording_ids=[BARE_ID], transcript_format="txt", notes_format="md"
            )
        )
    assert [output["status"] for output in caught.value.manifest["recordings"][0]["outputs"]] == [
        "skipped",
        "skipped",
    ]


def test_partial_manifest_redacts_renderer_errors(seeded_db, monkeypatch):
    def failed_transcript(*_args, **_kwargs):
        raise RuntimeError("Authorization: Bearer private-token-value")

    monkeypatch.setattr(bulk_export, "render_transcript_data", failed_transcript)
    payload = _archive_bytes(
        build_bulk_export(
            BulkExportRequest(
                recording_ids=[FIRST_ID], transcript_format="txt", notes_format="md"
            )
        )
    )
    _names, manifest, _files = _read_archive(payload)
    transcript = manifest["recordings"][0]["outputs"][0]
    assert transcript["status"] == "error"
    assert "private-token-value" not in transcript["message"]
    assert "[REDACTED]" in transcript["message"]


def test_missing_content_manifest_redacts_errors_and_other_lookup_errors_fail(seeded_db, monkeypatch):
    from localplaud.export_formats import MissingExportContentError

    def missing_transcript(*_args, **_kwargs):
        raise MissingExportContentError("Authorization: Bearer private-missing-token")

    monkeypatch.setattr(bulk_export, "render_transcript_data", missing_transcript)
    payload = _archive_bytes(
        build_bulk_export(
            BulkExportRequest(
                recording_ids=[FIRST_ID], transcript_format="txt", notes_format="md"
            )
        )
    )
    _names, manifest, _files = _read_archive(payload)
    transcript = manifest["recordings"][0]["outputs"][0]
    assert transcript["status"] == "skipped"
    assert "private-missing-token" not in transcript["message"]
    assert "[REDACTED]" in transcript["message"]

    def broken_transcript(*_args, **_kwargs):
        raise KeyError("Authorization: Bearer private-key-error")

    monkeypatch.setattr(bulk_export, "render_transcript_data", broken_transcript)
    payload = _archive_bytes(
        build_bulk_export(
            BulkExportRequest(
                recording_ids=[FIRST_ID], transcript_format="txt", notes_format="md"
            )
        )
    )
    _names, manifest, _files = _read_archive(payload)
    transcript = manifest["recordings"][0]["outputs"][0]
    assert transcript["status"] == "error"
    assert "private-key-error" not in transcript["message"]


def test_independent_renderer_excludes_cloud_and_stale_summary(seeded_db):
    payload = _archive_bytes(
        build_bulk_export(
            BulkExportRequest(
                recording_ids=[STALE_ID], transcript_format="txt", notes_format="txt"
            )
        )
    )
    names, manifest, files = _read_archive(payload)
    assert len(names) == 2
    combined = b"\n".join(files.values())
    assert b"canonical local transcript" in combined
    assert b"paid cloud transcript" not in combined
    assert b"paid cloud summary" not in combined
    assert b"stale local summary" not in combined
    assert [output["status"] for output in manifest["recordings"][0]["outputs"]] == [
        "emitted",
        "skipped",
    ]
    assert manifest["recordings"][0]["transcript_lineage"]["transcript_source"] == "local"


def test_per_entry_and_total_uncompressed_size_limits(seeded_db, monkeypatch):
    monkeypatch.setattr(
        bulk_export,
        "render_transcript_data",
        lambda *_args, **_kwargs: (b"t" * 5000, "text/plain"),
    )
    monkeypatch.setattr(
        bulk_export,
        "render_notes_data",
        lambda *_args, **_kwargs: (b"n" * 3000, "text/plain"),
    )
    monkeypatch.setattr(bulk_export, "MAX_ENTRY_UNCOMPRESSED_BYTES", 4000)
    payload = _archive_bytes(
        build_bulk_export(
            BulkExportRequest(
                recording_ids=[FIRST_ID], transcript_format="txt", notes_format="txt"
            )
        )
    )
    _names, manifest, _files = _read_archive(payload)
    outputs = manifest["recordings"][0]["outputs"]
    assert outputs[0]["error"] == "entry_size_limit_exceeded"
    assert outputs[1]["status"] == "emitted"

    monkeypatch.setattr(bulk_export, "MAX_ENTRY_UNCOMPRESSED_BYTES", 10_000)
    monkeypatch.setattr(bulk_export, "MAX_TOTAL_UNCOMPRESSED_BYTES", 5000)
    monkeypatch.setattr(
        bulk_export,
        "render_transcript_data",
        lambda *_args, **_kwargs: (b"t" * 3000, "text/plain"),
    )
    monkeypatch.setattr(
        bulk_export,
        "render_notes_data",
        lambda *_args, **_kwargs: (b"n" * 3000, "text/plain"),
    )
    payload = _archive_bytes(
        build_bulk_export(
            BulkExportRequest(
                recording_ids=[FIRST_ID], transcript_format="txt", notes_format="txt"
            )
        )
    )
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        assert sum(info.file_size for info in archive.infolist()) <= 5000
        manifest = json.loads(archive.read("manifest.json"))
    outputs = manifest["recordings"][0]["outputs"]
    assert outputs[0]["status"] == "emitted"
    assert outputs[1]["error"] == "total_size_limit_exceeded"


def test_builder_executes_no_database_mutations(seeded_db):
    statements: list[str] = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        result = build_bulk_export(
            BulkExportRequest(
                recording_ids=[FIRST_ID, SECOND_ID], transcript_format="txt", notes_format="md"
            )
        )
        result.close()
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert statements
    assert set(statements) == {"SELECT"}
