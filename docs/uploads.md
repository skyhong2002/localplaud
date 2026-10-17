# Local audio upload bounds and verification

`POST /api/imports/local/audio` accepts the existing MP3, MP4, M4A, WAV, Ogg,
Opus, WebM, FLAC, AAC, and AMR extensions. The file limit is 2 GiB. Empty files,
recognized containers with no audio stream, and media that an available ffprobe
cannot read are rejected with 422. The verified headerless Plaud Opus packet layout
remains accepted through the converter's existing validator. Unknown or non-finite
duration alone does not make a recording invalid.

An absent/unexecutable ffprobe or unavailable codec is recorded as
`media_probe: unavailable`; the original upload is retained for the normal processing
stage rather than mislabeled corrupt. Unexpected probe output is `inconclusive`.
A timeout returns 503 for a later retry. Successful probing reports `verified` (or
`verified_raw_opus`). Probe execution disables network protocols. Rejected uploads
and database failures remove the new audio and empty recording directory and close
the uploaded file.

## App integration hook

The endpoint's file check runs after multipart parsing. Install the ASGI middleware
to enforce a body bound **before** the multipart parser spools bytes:

```python
from localplaud.api.upload_limits import UploadBodyLimitMiddleware

app.add_middleware(UploadBodyLimitMiddleware)
```

Integration belongs in `api/app.py`, immediately after constructing the FastAPI app
and before registering its existing authentication middleware. Starlette runs the
last-added middleware first, so authentication then stays outside the limiter. This
scoped implementation supplies the hook without editing `app.py`; the main app is
not protected by the streaming bound until that registration is integrated.

The default request-body bound is 2 GiB + 64 KiB of multipart envelope overhead;
the endpoint still independently enforces the exact 2 GiB file limit. The limiter
handles the upload route, its trailing-slash form, and mounted/root-path deployments.
An excessive declared Content-Length is rejected without receiving a body. Missing
or understated lengths are bounded on every ASGI receive message, before the
crossing message reaches the parser. Earlier messages may have spooled within the
bound; a parser-compatible exception closes those temporary files before returning
413. Client disconnects and cancellation raised by ASGI receive use that same
cleanup. Cancellation is re-raised unchanged at the limiter boundary after the
parser closes partial files; no HTTP error response is sent for it. Remaining
request data is not drained.
The HTTP server may already have buffered a receive message; its transport buffering
and connection timeouts remain server concerns.

With an outer Starlette `BaseHTTPMiddleware`, the framework can turn cancellation
of its inner task into `RuntimeError("No response returned.")`. The limiter still
closes partial parser files and re-raises the original cancellation at its own
boundary; it does not change the outer middleware's cancellation policy.

Isolated regression commands (use the worktree interpreter/environment):

```bash
PYTHONPATH=src python -m pytest -q tests/test_upload_limits.py tests/test_imports.py
```
