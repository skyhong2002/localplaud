"""Deterministic, read-only bulk recording export archives."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from typing import Any, BinaryIO, Literal, TypeAlias

from .error_redaction import sanitize_error
from .export_formats import (
    MissingExportContentError,
    recording_data,
    render_notes_data,
    render_transcript_data,
)

TranscriptFormat: TypeAlias = Literal["txt", "srt", "vtt", "docx", "pdf"]
NotesFormat: TypeAlias = Literal["md", "txt", "docx", "pdf"]

MAX_RECORDINGS = 50
MAX_ENTRY_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
SPOOL_MAX_MEMORY_BYTES = 8 * 1024 * 1024
MAX_TITLE_BYTES = 96

_TRANSCRIPT_FORMATS = frozenset({"txt", "srt", "vtt", "docx", "pdf"})
_NOTES_FORMATS = frozenset({"md", "txt", "docx", "pdf"})
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_PDF_DATE_RE = re.compile(rb"/(CreationDate|ModDate) \((D:[^)]*)\)")
_PDF_ID_RE = re.compile(
    rb"(/ID\s*\[\s*)<([0-9A-Fa-f]+)><([0-9A-Fa-f]+)>(\s*\])"
)
_DOCX_DATE_RE = re.compile(
    rb"(<dcterms:(?:created|modified)\b[^>]*>)[^<]*(</dcterms:(?:created|modified)>)"
)


class BulkExportError(Exception):
    """Base error for callers translating archive failures to API responses."""


class BulkExportValidationError(BulkExportError, ValueError):
    """The requested selection or format options are invalid."""


class UnknownRecordingIdsError(BulkExportError, LookupError):
    """One or more recording IDs do not exist."""

    def __init__(self, recording_ids: tuple[str, ...]) -> None:
        self.recording_ids = recording_ids
        super().__init__(f"unknown recording IDs: {', '.join(recording_ids)}")


class NoExportableContentError(BulkExportError, LookupError):
    """Every requested output was missing or failed validation/rendering."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        self.manifest = manifest
        super().__init__("none of the selected recordings has exportable requested content")


@dataclass(frozen=True, slots=True)
class BulkExportRequest:
    """Validated by :func:`build_bulk_export` for convenient route construction."""

    recording_ids: tuple[str, ...] | list[str]
    transcript_format: TranscriptFormat | None = None
    notes_format: NotesFormat | None = None
    timestamps: bool = True
    speakers: bool = True


@dataclass(slots=True)
class BulkExportResult:
    """A seekable archive stream and metadata suitable for a streaming response."""

    stream: BinaryIO
    size_bytes: int
    manifest: dict[str, Any]
    filename: str = "localplaud-recordings.zip"
    media_type: str = "application/zip"

    def __iter__(self) -> Iterator[bytes]:
        while chunk := self.stream.read(1024 * 1024):
            yield chunk

    def close(self) -> None:
        self.stream.close()

    def __enter__(self) -> BulkExportResult:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(slots=True)
class _PendingEntry:
    path: str
    stream: BinaryIO
    size_bytes: int
    output_record: dict[str, Any]


def _validated_request(request: BulkExportRequest) -> tuple[str, ...]:
    if isinstance(request.recording_ids, str):
        raise BulkExportValidationError("recording_ids must be a sequence of IDs")
    if not all(isinstance(recording_id, str) and recording_id for recording_id in request.recording_ids):
        raise BulkExportValidationError("recording IDs must be non-empty strings")
    recording_ids = tuple(dict.fromkeys(request.recording_ids))
    if not recording_ids:
        raise BulkExportValidationError("select at least one recording")
    if len(recording_ids) > MAX_RECORDINGS:
        raise BulkExportValidationError(f"select no more than {MAX_RECORDINGS} recordings")
    if request.transcript_format is None and request.notes_format is None:
        raise BulkExportValidationError("select at least one content type")
    if request.transcript_format is not None and request.transcript_format not in _TRANSCRIPT_FORMATS:
        raise BulkExportValidationError("unsupported transcript format")
    if request.notes_format is not None and request.notes_format not in _NOTES_FORMATS:
        raise BulkExportValidationError("unsupported notes format")
    if not isinstance(request.timestamps, bool) or not isinstance(request.speakers, bool):
        raise BulkExportValidationError("timestamps and speakers must be booleans")
    return recording_ids


def _truncate_utf8(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[:limit].decode("utf-8", errors="ignore")


def _safe_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    characters: list[str] = []
    separator = False
    for character in normalized:
        if character.isalnum() or character in {"-", "_"}:
            characters.append(character)
            separator = False
        elif not separator:
            characters.append("-")
            separator = True
    title = "".join(characters).strip("-_.") or "recording"
    return _truncate_utf8(title, MAX_TITLE_BYTES).rstrip("-_.") or "recording"


def _entry_stem(recording_id: str, title: str) -> str:
    stable_id = hashlib.sha256(recording_id.encode("utf-8")).hexdigest()[:12]
    return f"{_safe_title(title)}--{stable_id}"


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100600 << 16
    return info


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _deterministic_pdf(payload: bytes) -> bytes:
    """Remove ReportLab's run-specific metadata without changing PDF offsets."""

    def replace_date(match: re.Match[bytes]) -> bytes:
        value = match.group(2)
        stable = b"D:20000101000000+00'00'"
        if len(stable) != len(value):
            stable = b"D:" + (b"0" * (len(value) - 2))
        return b"/" + match.group(1) + b" (" + stable + b")"

    def replace_id(match: re.Match[bytes]) -> bytes:
        return (
            match.group(1)
            + b"<"
            + (b"0" * len(match.group(2)))
            + b"><"
            + (b"0" * len(match.group(3)))
            + b">"
            + match.group(4)
        )

    return _PDF_ID_RE.sub(replace_id, _PDF_DATE_RE.sub(replace_date, payload))


def _deterministic_docx(payload: bytes) -> bytes:
    """Repack DOCX members with stable timestamps and core metadata."""
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(payload)) as source, zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=False,
    ) as archive:
        for source_info in source.infolist():
            content = source.read(source_info.filename)
            if source_info.filename == "docProps/core.xml":
                content = _DOCX_DATE_RE.sub(
                    rb"\g<1>2000-01-01T00:00:00Z\g<2>", content
                )
            info = zipfile.ZipInfo(source_info.filename, date_time=_ZIP_TIMESTAMP)
            info.compress_type = source_info.compress_type
            info.create_system = source_info.create_system
            info.external_attr = source_info.external_attr
            archive.writestr(info, content)
    return output.getvalue()


def _mark_output_error(
    output: dict[str, Any], code: str, message: str, *, attempted_size: int | None = None
) -> None:
    content_type = output["content_type"]
    output_format = output["format"]
    output.clear()
    output.update(
        {
            "content_type": content_type,
            "format": output_format,
            "status": "error",
            "error": code,
            "message": message,
        }
    )
    if attempted_size is not None:
        output["attempted_size_bytes"] = attempted_size


def _render_output(
    data: dict[str, Any],
    content_type: str,
    output_format: str,
    request: BulkExportRequest,
) -> tuple[bytes, str]:
    if content_type == "transcript":
        return render_transcript_data(
            data,
            output_format,
            timestamps=request.timestamps,
            speakers=request.speakers,
        )
    return render_notes_data(data, output_format)


def build_bulk_export(request: BulkExportRequest) -> BulkExportResult:
    """Build a bounded ZIP without mutating recordings or exporting audio."""
    recording_ids = _validated_request(request)

    prepared: list[tuple[str, dict[str, Any]]] = []
    unknown_ids: list[str] = []
    for recording_id in recording_ids:
        try:
            prepared.append((recording_id, recording_data(recording_id)))
        except ValueError:
            unknown_ids.append(recording_id)
    if unknown_ids:
        raise UnknownRecordingIdsError(tuple(unknown_ids))

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "requested_options": {
            "recording_ids": list(recording_ids),
            "transcript_format": request.transcript_format,
            "notes_format": request.notes_format,
            "timestamps": request.timestamps,
            "speakers": request.speakers,
        },
        "recordings": [],
    }
    pending: list[_PendingEntry] = []
    content_total = 0

    selections = (
        ("transcript", request.transcript_format),
        ("notes", request.notes_format),
    )
    try:
        for recording_id, data in prepared:
            recording_result = {
                "id": recording_id,
                "title": str(data["title"]),
                "transcript_lineage": dict(data.get("transcript_provenance") or {}),
                "outputs": [],
            }
            manifest["recordings"].append(recording_result)
            stem = _entry_stem(recording_id, str(data["title"]))

            for content_type, output_format in selections:
                if output_format is None:
                    continue
                output: dict[str, Any] = {
                    "content_type": content_type,
                    "format": output_format,
                }
                recording_result["outputs"].append(output)
                path = f"recordings/{stem}/{content_type}.{output_format}"
                try:
                    payload, media_type = _render_output(data, content_type, output_format, request)
                except MissingExportContentError as exc:
                    output.update(
                        {
                            "status": "skipped",
                            "reason": "missing_content",
                            "message": sanitize_error(exc, max_length=500),
                        }
                    )
                    continue
                except Exception as exc:
                    output.update(
                        {
                            "status": "error",
                            "error": "render_failed",
                            "message": sanitize_error(exc, max_length=500)
                            or type(exc).__name__,
                        }
                    )
                    continue

                if output_format == "pdf":
                    payload = _deterministic_pdf(payload)
                elif output_format == "docx":
                    payload = _deterministic_docx(payload)

                size_bytes = len(payload)
                if size_bytes > MAX_ENTRY_UNCOMPRESSED_BYTES:
                    _mark_output_error(
                        output,
                        "entry_size_limit_exceeded",
                        "rendered output exceeds the per-entry uncompressed size limit",
                        attempted_size=size_bytes,
                    )
                    continue
                if content_total + size_bytes > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    _mark_output_error(
                        output,
                        "total_size_limit_exceeded",
                        "rendered output exceeds the archive uncompressed size limit",
                        attempted_size=size_bytes,
                    )
                    continue

                entry_stream = tempfile.SpooledTemporaryFile(
                    max_size=SPOOL_MAX_MEMORY_BYTES, mode="w+b"
                )
                entry_stream.write(payload)
                entry_stream.seek(0)
                output.update(
                    {
                        "status": "emitted",
                        "path": path,
                        "media_type": media_type,
                        "size_bytes": size_bytes,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
                pending.append(_PendingEntry(path, entry_stream, size_bytes, output))
                content_total += size_bytes

        manifest_payload = _manifest_bytes(manifest)
        while pending and content_total + len(manifest_payload) > MAX_TOTAL_UNCOMPRESSED_BYTES:
            removed = pending.pop()
            content_total -= removed.size_bytes
            removed.stream.close()
            _mark_output_error(
                removed.output_record,
                "total_size_limit_exceeded",
                "output was omitted so the manifest fits the archive size limit",
                attempted_size=removed.size_bytes,
            )
            manifest_payload = _manifest_bytes(manifest)

        if not pending:
            raise NoExportableContentError(manifest)

        archive_stream = tempfile.SpooledTemporaryFile(
            max_size=SPOOL_MAX_MEMORY_BYTES, mode="w+b"
        )
        try:
            with zipfile.ZipFile(
                archive_stream,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
                allowZip64=False,
            ) as archive:
                for entry in pending:
                    entry.stream.seek(0)
                    with archive.open(_zip_info(entry.path), mode="w") as destination:
                        shutil.copyfileobj(entry.stream, destination, length=1024 * 1024)
                archive.writestr(_zip_info("manifest.json"), manifest_payload)
            size_bytes = archive_stream.tell()
            archive_stream.seek(0)
            return BulkExportResult(
                stream=archive_stream,
                size_bytes=size_bytes,
                manifest=manifest,
            )
        except Exception:
            archive_stream.close()
            raise
    finally:
        for entry in pending:
            entry.stream.close()
