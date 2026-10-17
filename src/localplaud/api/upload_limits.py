"""Bound upload bodies before Starlette's multipart parser can spool them.

Integration: ``app.add_middleware(UploadBodyLimitMiddleware)``. Keep authentication
outside this middleware when ordering the app stack. The endpoint independently
checks file bytes; this bound includes a small allowance for the multipart envelope.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any

from anyio import CancelScope
from fastapi.routing import APIRoute
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser, parse_options_header
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 64 * 1024
UPLOAD_PATH = "/api/imports/local/audio"


class _UploadFile(UploadFile):
    async def _run_io(self, operation: Coroutine[Any, Any, None]) -> None:
        async def run() -> BaseException | None:
            try:
                await operation
            except BaseException as exc:
                # Return cancellation as a value: asyncio.shield otherwise
                # replaces a cancelled child's original CancelledError.
                return exc
            return None

        # Own the I/O independently of request cancellation. In particular, a
        # worker rolling the spool to disk must finish before parser cleanup
        # closes it, or it could install a new disk file AFTER that close.
        task = asyncio.create_task(run())
        try:
            error = await asyncio.shield(task)
        except asyncio.CancelledError:
            # AnyIO scope shielding prevents level cancellation from spinning;
            # asyncio.shield plus retrying also handles repeated Task.cancel().
            with CancelScope(shield=True):
                while not task.done():
                    with suppress(asyncio.CancelledError):
                        await asyncio.shield(task)
            # The worker's error must not replace the request's cancellation.
            # run() captures it, so completion cannot leave an unobserved error.
            raise
        if error is not None:
            raise error

    async def write(self, data: bytes) -> None:
        await self._run_io(super().write(data))

    async def seek(self, offset: int) -> None:
        await self._run_io(super().seek(offset))


class _UploadMultiPartParser(MultiPartParser):
    def on_headers_finished(self) -> None:
        super().on_headers_finished()
        upload = self._current_part.file
        if upload is not None:
            # Keep Starlette's header validation, spool construction, and error
            # file tracking, changing only this route's UploadFile lifecycle.
            self._current_part.file = _UploadFile(
                file=upload.file,
                size=upload.size,
                filename=upload.filename,
                headers=upload.headers,
            )

    async def parse(self) -> FormData:
        try:
            return await super().parse()
        except BaseException:
            # Starlette's error list includes completed and in-progress files,
            # but its handler omits cancellation during UploadFile.write/seek.
            # UploadFile.write/seek have drained their worker before unwinding.
            # Close synchronously: another await could itself be cancelled.
            for file in self._files_to_close_on_error:
                with suppress(Exception):
                    file.close()
            raise


class _UploadRequest(Request):
    async def _get_form(
        self,
        *,
        max_files: int | float = 1000,
        max_fields: int | float = 1000,
        max_part_size: int = 1024 * 1024,
    ) -> FormData:
        if (
            self._form is None
            and parse_options_header(self.headers.get("content-type"))[0] == b"multipart/form-data"
        ):
            parser = _UploadMultiPartParser(
                self.headers,
                self.stream(),
                max_files=max_files,
                max_fields=max_fields,
                max_part_size=max_part_size,
            )
            try:
                self._form = await parser.parse()
            except MultiPartException as exc:
                # Keep Request._get_form's normal parser-error response behavior.
                raise HTTPException(status_code=400, detail=exc.message) from exc
        return await super()._get_form(
            max_files=max_files, max_fields=max_fields, max_part_size=max_part_size
        )


class UploadCleanupRoute(APIRoute):
    """Install cancellation-safe multipart parsing only on the upload endpoint."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def upload_handler(request: Request) -> Response:
            return await handler(_UploadRequest(request.scope, request.receive))

        return upload_handler


class _UploadBodyTooLarge(MultiPartException):
    """Parser-compatible failure: Starlette closes every partial spool on exit."""


class _UploadDisconnected(MultiPartException):
    """Use the same parser cleanup when a client abandons a multipart upload."""


class UploadBodyLimitMiddleware:
    def __init__(
        self, app: ASGIApp, *, max_body_bytes: int = MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES
    ) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and (path == root or path.startswith(root.rstrip("/") + "/")):
            path = path[len(root) :]
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or path.rstrip("/") != UPLOAD_PATH
        ):
            await self.app(scope, receive, send)
            return

        rejection = JSONResponse(status_code=413, content={"detail": "upload body exceeds limit"})
        lengths = [
            value for key, value in scope.get("headers", []) if key.lower() == b"content-length"
        ]
        if lengths:
            if any(not value or not value.isdigit() for value in lengths) or len(set(lengths)) != 1:
                await JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})(
                    scope, receive, send
                )
                return
            # Compare decimal strings first, avoiding integer conversion limits for
            # malicious headers containing thousands of digits.
            length = lengths[0].lstrip(b"0") or b"0"
            limit = str(self.max_body_bytes).encode("ascii")
            if len(length) > len(limit) or (len(length) == len(limit) and length > limit):
                await rejection(scope, receive, send)
                return

        received = 0
        exceeded = False
        rejected = False
        disconnected = False
        cancellation = None

        async def bounded_receive() -> Message:
            nonlocal received, exceeded, disconnected, cancellation
            try:
                message = await receive()
            except asyncio.CancelledError as exc:
                # Starlette only closes partial parser files on MultiPartException
                # (or OSError). Let that synchronous cleanup run before restoring
                # the original cancellation at the middleware boundary.
                cancellation = exc
                disconnected = True
                raise _UploadDisconnected("upload cancelled") from exc
            if message["type"] == "http.disconnect":
                disconnected = True
                raise _UploadDisconnected("upload disconnected")
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    exceeded = True
                    # A plain HTTPException would bypass the parser's cleanup.
                    raise _UploadBodyTooLarge("upload body exceeds limit")
            return message

        async def bounded_send(message: Message) -> None:
            nonlocal rejected
            if disconnected:
                return
            if exceeded:
                # Request._get_form translates MultiPartException to HTTP 400;
                # replace that response with 413 after its spool cleanup completes.
                if not rejected:
                    rejected = True
                    await rejection(scope, receive, send)
                return
            await send(message)

        try:
            await self.app(scope, bounded_receive, bounded_send)
        except _UploadBodyTooLarge:
            if not rejected:
                await rejection(scope, receive, send)
        except _UploadDisconnected:
            return
        finally:
            if cancellation is not None:
                raise cancellation
