"""Shared responses for locally stored recording audio."""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse

_AUDIO_MIME = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
}


def audio_file_response(path: str | Path, *, filename: str | None = None) -> FileResponse:
    """Serve audio with Starlette's conditional and HTTP Range support."""
    audio_path = Path(path)
    extension = audio_path.suffix.lstrip(".").lower()
    return FileResponse(
        audio_path,
        media_type=_AUDIO_MIME.get(extension, "application/octet-stream"),
        filename=filename,
    )
