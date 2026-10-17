"""Exercise the ASGI receive boundary and real multipart spool cleanup."""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, UploadFile

from localplaud.api import imports
from localplaud.api.upload_limits import UPLOAD_PATH, UploadBodyLimitMiddleware


def _request(app, chunks, *, path=UPLOAD_PATH, content_length=None):
    messages = []
    consumed = []

    async def exercise():
        pending = list(chunks)

        async def receive():
            if not pending:
                return {"type": "http.disconnect"}
            chunk = pending.pop(0)
            if chunk is None:
                return {"type": "http.disconnect"}
            consumed.append(chunk)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

        async def send(message):
            messages.append(message)

        headers = [(b"content-type", b"multipart/form-data; boundary=fixture")]
        if content_length is not None:
            headers.append((b"content-length", content_length))
        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "root_path": "",
                "scheme": "http",
                "server": ("fixture", 80),
                "client": ("fixture", 123),
                "headers": headers,
            },
            receive,
            send,
        )

    asyncio.run(exercise())
    return messages, consumed


@pytest.mark.parametrize("declared", [None, b"10"])
@pytest.mark.parametrize("outer_middleware", [False, True])
def test_streaming_limit_rejects_unknown_or_understated_length_and_closes_spool(
    monkeypatch, declared, outer_middleware
):
    import starlette.formparsers as parser

    spools = []

    def tracked_spool(*args, **kwargs):
        kwargs["max_size"] = 1
        spool = tempfile.SpooledTemporaryFile(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(parser, "SpooledTemporaryFile", tracked_spool)
    app = FastAPI()
    app.include_router(imports.router)
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=256)
    if outer_middleware:

        @app.middleware("http")
        async def pass_through(request, call_next):
            return await call_next(request)

    monkeypatch.setattr(imports, "file_dir", lambda _id: pytest.fail("endpoint ran after limit"))
    beginning = (
        b'--fixture\r\nContent-Disposition: form-data; name="file"; filename="large.wav"\r\nContent-Type: audio/wav\r\n\r\n'
        + b"a" * 16
    )
    oversized = b"b" * 300
    messages, consumed = _request(
        app, [beginning, oversized, b"never consumed"], content_length=declared
    )
    assert messages[0]["status"] == 413
    assert json.loads(messages[1]["body"])["detail"] == "upload body exceeds limit"
    assert consumed == [beginning, oversized]
    assert spools and all(spool.closed for spool in spools)


def test_disconnect_closes_partial_multipart_spools(monkeypatch):
    import starlette.formparsers as parser

    spools = []

    def tracked_spool(*args, **kwargs):
        spool = tempfile.SpooledTemporaryFile(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(parser, "SpooledTemporaryFile", tracked_spool)
    app = FastAPI()
    app.include_router(imports.router)
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=1024)
    beginning = b'--fixture\r\nContent-Disposition: form-data; name="file"; filename="partial.wav"\r\n\r\nfixture'
    messages, consumed = _request(app, [beginning, None])
    assert messages == [] and consumed == [beginning]
    assert spools and all(spool.closed for spool in spools)


def test_mounted_upload_route_is_bounded():
    child = FastAPI()
    child.include_router(imports.router)
    child.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=256)
    parent = FastAPI()
    parent.mount("/service", child)
    messages, consumed = _request(
        parent, [b"never read"], path="/service" + UPLOAD_PATH, content_length=b"257"
    )
    assert messages[0]["status"] == 413 and consumed == []


@pytest.mark.parametrize("path", [UPLOAD_PATH, UPLOAD_PATH + "/"])
def test_declared_oversize_rejected_without_receiving_or_spooling(path):
    async def endpoint(*_args):
        pytest.fail("oversized request reached app")

    messages, consumed = _request(
        UploadBodyLimitMiddleware(endpoint, max_body_bytes=256),
        [b"never read"],
        path=path,
        content_length=b"257",
    )
    assert messages[0]["status"] == 413
    assert consumed == []


@pytest.mark.parametrize("length", [b"-1", b"wat", b"", b"9" * 5000])
def test_malformed_or_huge_length_is_handled_without_reading_body(length):
    async def endpoint(*_args):
        pytest.fail("invalid length reached app")

    messages, consumed = _request(
        UploadBodyLimitMiddleware(endpoint, max_body_bytes=256), [b"unused"], content_length=length
    )
    assert messages[0]["status"] == (413 if length == b"9" * 5000 else 400)
    assert consumed == []


def test_exact_body_limit_and_other_routes_are_unaffected():
    seen = []

    async def endpoint(scope, receive, send):
        seen.append((scope["path"], await receive()))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = UploadBodyLimitMiddleware(endpoint, max_body_bytes=4)
    messages, consumed = _request(app, [b"1234"], content_length=b"0004")
    assert messages[0]["status"] == 204 and consumed == [b"1234"]
    messages, _ = _request(app, [b"123456"], path="/unrelated", content_length=b"6")
    assert messages[0]["status"] == 204
    assert len(seen) == 2


def test_small_multipart_reaches_endpoint_and_file_limit_remains_independent(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(imports.router)
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=1024)
    monkeypatch.setattr(imports, "_MAX_UPLOAD_BYTES", 3)
    directory = tmp_path / "recording"

    def file_dir(_id):
        directory.mkdir()
        return directory

    monkeypatch.setattr(imports, "file_dir", file_dir)
    response = TestClient(app).post(UPLOAD_PATH, files={"file": ("test.wav", b"1234")})
    assert response.status_code == 413
    assert not directory.exists()


@pytest.mark.parametrize("failure", ["empty", "corrupt", "no_audio", "timeout", "database"])
def test_upload_failures_leave_no_audio_or_directory_and_close_upload(
    monkeypatch, tmp_path, failure
):
    directory = tmp_path / "recording"

    def file_dir(_id):
        directory.mkdir()
        return directory

    monkeypatch.setattr(imports, "file_dir", file_dir)

    def probe(*_args, **_kwargs):
        if failure == "corrupt":
            raise subprocess.CalledProcessError(
                1, "ffprobe", stderr="Invalid data found when processing input"
            )
        if failure == "timeout":
            raise subprocess.TimeoutExpired("ffprobe", 30)
        streams = [] if failure == "no_audio" else [{"codec_type": "audio"}]
        return SimpleNamespace(
            stdout=json.dumps({"streams": streams, "format": {"duration": "1.25"}})
        )

    @contextmanager
    def failed_database():
        yield SimpleNamespace(add=lambda _row: None)
        raise RuntimeError("fixture database failure")

    monkeypatch.setattr(imports.subprocess, "run", probe)
    monkeypatch.setattr(imports, "session_scope", failed_database)
    upload = UploadFile(
        filename="fixture.wav", file=BytesIO(b"" if failure == "empty" else b"fixture")
    )
    expected = RuntimeError if failure == "database" else HTTPException
    with pytest.raises(expected) as error:
        imports.import_local_audio(upload)
    if isinstance(error.value, HTTPException):
        assert error.value.status_code == (503 if failure == "timeout" else 422)
    assert upload.file.closed and not directory.exists()


@pytest.mark.parametrize("extension", sorted(imports._ALLOWED_EXTENSIONS))
def test_missing_probe_preserves_every_previously_supported_extension(
    monkeypatch, tmp_path, extension
):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("fixture ffprobe unavailable")

    @contextmanager
    def database():
        yield SimpleNamespace(add=lambda row: rows.append(row))

    directory = tmp_path / "recording"
    directory.mkdir()
    rows = []
    monkeypatch.setattr(imports, "file_dir", lambda _id: directory)
    monkeypatch.setattr(imports.subprocess, "run", missing)
    monkeypatch.setattr(imports, "session_scope", database)
    upload = UploadFile(filename=f"memo.{extension}", file=BytesIO(b"fixture bytes"))
    result = imports.import_local_audio(upload)
    assert result["duration_ms"] is None and result["media_probe"] == "unavailable"
    assert rows[0].raw["media_probe"] == "unavailable"
    assert upload.file.closed
    assert (directory / f"audio.{extension}").read_bytes() == b"fixture bytes"


@pytest.mark.parametrize("duration", ["N/A", "nan", "inf", "-1", None])
def test_recognized_audio_with_unknown_duration_is_accepted(monkeypatch, tmp_path, duration):
    monkeypatch.setattr(
        imports.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=json.dumps(
                {
                    "streams": [{"codec_type": "audio"}],
                    "format": {"duration": duration},
                }
            )
        ),
    )
    probe = imports._probe_audio(tmp_path / "memo.amr")
    assert probe.status == "verified" and probe.duration_ms is None


def test_verified_headerless_opus_remains_supported(monkeypatch, tmp_path):
    def failed(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            1, "ffprobe", stderr="Invalid data found when processing input"
        )

    monkeypatch.setattr(imports.subprocess, "run", failed)
    recording = tmp_path / "audio.opus"
    recording.write_bytes((b"\xb8" + b"\x00" * 79) * 50)
    assert imports._probe_audio(recording) == imports._AudioProbe(1000, "verified_raw_opus")
