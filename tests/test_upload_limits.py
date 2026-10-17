"""Exercise the ASGI receive boundary and real multipart spool cleanup."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import threading
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
            if isinstance(chunk, BaseException):
                raise chunk
            if callable(chunk):
                await chunk()
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


@pytest.mark.parametrize("cancel_task", [False, True])
def test_receive_cancellation_closes_rolled_multipart_spools_and_propagates(
    monkeypatch, cancel_task
):
    import starlette.formparsers as parser

    spools = []
    sent = []

    def tracked_spool(*args, **kwargs):
        kwargs["max_size"] = 1
        spool = tempfile.SpooledTemporaryFile(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(parser, "SpooledTemporaryFile", tracked_spool)
    monkeypatch.setattr(imports, "file_dir", lambda _id: pytest.fail("cancelled upload ran"))
    app = FastAPI()
    app.include_router(imports.router)
    app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=4096)

    async def record_send(scope, receive, send):
        async def tracked_send(message):
            sent.append(message)
            await send(message)

        await app(scope, receive, tracked_send)

    async def cancel_current_task():
        asyncio.current_task().cancel("synthetic receive cancellation")
        await asyncio.sleep(0)

    cancellation = asyncio.CancelledError("synthetic receive cancellation")
    beginning = b'--fixture\r\nContent-Disposition: form-data; name="file"; filename="partial.wav"\r\n\r\nfixture'
    with pytest.raises(asyncio.CancelledError, match="synthetic receive cancellation") as exc:
        _request(record_send, [beginning, cancel_current_task if cancel_task else cancellation])
    if not cancel_task:
        assert exc.value is cancellation
    assert sent == []
    assert spools and all(spool._rolled and spool.closed for spool in spools)


@pytest.mark.parametrize("operation", ["write", "seek"])
@pytest.mark.parametrize("cancel_task", [False, True])
@pytest.mark.parametrize("with_limiter", [False, True])
def test_parser_io_cancellation_closes_all_rolled_spools_and_preserves_cancellation(
    monkeypatch, operation, cancel_task, with_limiter
):
    import starlette.formparsers as parser
    from starlette.datastructures import UploadFile as ParserUploadFile

    spools = []
    sent = []
    raised = []
    cancellation = asyncio.CancelledError(f"cancel during multipart {operation}")
    actual_operation = getattr(ParserUploadFile, operation)

    def tracked_spool(*args, **kwargs):
        kwargs["max_size"] = 1
        spool = tempfile.SpooledTemporaryFile(*args, **kwargs)
        spools.append(spool)
        return spool

    async def cancel_during_io(upload, *args):
        await actual_operation(upload, *args)
        if upload.filename != "partial.wav":
            return
        assert upload.file._rolled and not upload.file.closed
        try:
            if cancel_task:
                asyncio.current_task().cancel(str(cancellation))
                await asyncio.sleep(0)
            else:
                raise cancellation
        except asyncio.CancelledError as exc:
            raised.append(exc)
            raise

    monkeypatch.setattr(parser, "SpooledTemporaryFile", tracked_spool)
    monkeypatch.setattr(ParserUploadFile, operation, cancel_during_io)
    monkeypatch.setattr(imports, "file_dir", lambda _id: pytest.fail("cancelled upload ran"))
    app = FastAPI()
    app.include_router(imports.router)
    if with_limiter:
        app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=4096)

    async def record_send(scope, receive, send):
        async def tracked_send(message):
            sent.append(message)
            await send(message)

        await app(scope, receive, tracked_send)

    # One completed file and one failing file must both close. Use separate
    # receive chunks so the first file has finished its write AND seek already.
    completed = b'--fixture\r\nContent-Disposition: form-data; name="extra"; filename="complete.wav"\r\n\r\ncompleted\r\n--fixture\r\n'
    failing = b'Content-Disposition: form-data; name="file"; filename="partial.wav"\r\n\r\nfixture\r\n--fixture--\r\n'
    with pytest.raises(asyncio.CancelledError, match=str(cancellation)) as exc:
        _request(record_send, [completed, failing])
    assert raised == [exc.value]
    if not cancel_task:
        assert exc.value is cancellation
    assert sent == []
    assert len(spools) == 2 and all(spool._rolled and spool.closed for spool in spools)


@pytest.mark.parametrize("operation", ["rollover", "write", "seek", "seek_error"])
@pytest.mark.parametrize("cancellation", ["direct", "repeated", "scope"])
@pytest.mark.parametrize("with_limiter", [False, True])
def test_inflight_threaded_upload_io_drains_before_close(
    monkeypatch, operation, cancellation, with_limiter
):
    import anyio
    import starlette.formparsers as parser

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    spools, physical_files, descriptors, sent, raised = [], [], [], [], []
    actual_spool, actual_tmpfile = tempfile.SpooledTemporaryFile, tempfile.TemporaryFile
    event_loop_thread = threading.get_ident()

    def pause_worker():
        assert threading.get_ident() != event_loop_thread
        entered.set()
        if not release.wait(5):
            raise AssertionError("fixture did not release the I/O worker")

    def tracked_tmpfile(*args, **kwargs):
        file = actual_tmpfile(*args, **kwargs)
        physical_files.append(file)
        descriptors.append(file.fileno())
        if operation == "rollover":
            # Pause after creating the disk file, before rollover installs it.
            pause_worker()
        return file

    def tracked_spool(*args, **kwargs):
        spool = actual_spool(*args, **kwargs)
        spools.append(spool)
        actual_io = spool.seek if operation.startswith("seek") else spool.write

        def tracked_io(*args):
            try:
                if operation.startswith("seek") or (operation == "write" and spool._rolled):
                    pause_worker()
                if operation == "seek_error":
                    raise OSError("worker failure must not replace cancellation")
                return actual_io(*args)
            finally:
                if entered.is_set():
                    finished.set()

        if operation.startswith("seek"):
            spool.seek = tracked_io
        else:
            spool.write = tracked_io
        return spool

    monkeypatch.setattr(parser, "SpooledTemporaryFile", tracked_spool)
    monkeypatch.setattr(tempfile, "TemporaryFile", tracked_tmpfile)
    monkeypatch.setattr(imports, "file_dir", lambda _id: pytest.fail("cancelled upload ran"))
    app = FastAPI()
    app.include_router(imports.router)
    if with_limiter:
        app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=2 * 1024 * 1024)

    async def exercise():
        beginning = (
            b'--fixture\r\nContent-Disposition: form-data; name="file"; filename="memo.wav"\r\n\r\n'
            + b"x" * (1024 * 1024 + 1)
        )
        ending = b"y\r\n--fixture--\r\n"
        chunks = [beginning, ending] if operation == "write" else [beginning + ending]
        receives = []
        scopes = []

        async def receive():
            receives.append(True)
            assert chunks
            return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}

        async def send(message):
            sent.append(message)

        async def request():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                try:
                    await app(
                        {
                            "type": "http",
                            "asgi": {"version": "3.0"},
                            "http_version": "1.1",
                            "method": "POST",
                            "path": UPLOAD_PATH,
                            "raw_path": UPLOAD_PATH.encode(),
                            "query_string": b"",
                            "root_path": "",
                            "scheme": "http",
                            "server": ("fixture", 80),
                            "client": ("fixture", 1),
                            "headers": [(b"content-type", b"multipart/form-data; boundary=fixture")],
                        },
                        receive,
                        send,
                    )
                except asyncio.CancelledError as exc:
                    raised.append(exc)
                    raise

        task = asyncio.create_task(request())
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            if cancellation == "scope":
                scopes[0].cancel()
            else:
                task.cancel("original in-flight cancellation")
            # Let the request enter cancellation cleanup while the worker is
            # paused. Release independently BEFORE awaiting request completion:
            # a correct implementation must wait for this worker to finish.
            for _ in range(3):
                await asyncio.sleep(0)
            if cancellation == "repeated":
                for _ in range(3):
                    task.cancel("later cancellation must not replace the original")
                    await asyncio.sleep(0)
            pending_before_release = not task.done()
            open_before_release = all(not spool.closed for spool in spools)
            release.set()
            if cancellation == "scope":
                await asyncio.wait_for(task, 3)
            else:
                with pytest.raises(asyncio.CancelledError) as error:
                    await asyncio.wait_for(task, 3)
                assert str(error.value) == "original in-flight cancellation"
                assert raised == [error.value]
            async with asyncio.timeout(3):
                while not finished.is_set():
                    await asyncio.sleep(0.001)
            assert len(raised) == 1 and not sent
            assert receives == [True] * (2 if operation == "write" else 1)
            assert len(spools) == 1 and spools[0]._max_size == 1024 * 1024
            assert all(spool._rolled and spool.closed for spool in spools)
            assert len(physical_files) == 1 and all(file.closed for file in physical_files)
            for descriptor in descriptors:
                with pytest.raises(OSError):
                    os.fstat(descriptor)
            assert pending_before_release and open_before_release
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
            for file in physical_files:
                file.close()
            for spool in spools:
                spool.close()

    asyncio.run(exercise())


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
