"""Read-only client for the Plaud cloud API.

Only GET requests — localplaud never mutates cloud data. Endpoints confirmed
by reverse engineering (see docs/plaud-api.md):

- ``GET /user/me``                     — auth validation
- ``GET /file/simple/web``             — file list (paged)
- ``GET /file/detail/{id}``            — transcript + summary + metadata

The signed audio-download URL is resolved from the file-detail payload; see
``resolve_audio_url`` for the strategy and the fallback candidates.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import PlaudConfig
from .auth import build_client
from .models import FileListResponse, PlaudFileDTO

log = logging.getLogger(__name__)


class PlaudError(RuntimeError):
    pass


class PlaudAuthError(PlaudError):
    pass


# Keys in the file-detail payload that may carry a downloadable audio URL.
# The exact key is still being confirmed; we scan defensively.
_AUDIO_URL_HINTS = ("audio_url", "file_url", "url", "download_url", "oss_url", "cos_url", "cdn_url")


class PlaudClient:
    def __init__(self, cfg: PlaudConfig):
        self.cfg = cfg
        self._client: httpx.Client = build_client(cfg)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PlaudClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- low level ----------------------------------------------------- #

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10), reraise=True)
    def _get(self, path: str, **params) -> httpx.Response:
        resp = self._client.get(path, params=params or None)
        if resp.status_code in (401, 403):
            raise PlaudAuthError(
                f"Plaud API returned {resp.status_code} for {path} — your session "
                "is missing/expired. Re-copy an authenticated request (see README)."
            )
        resp.raise_for_status()
        return resp

    def _get_json(self, path: str, **params) -> dict:
        data = self._get(path, **params).json()
        # Plaud wraps responses as {status, msg, data...}; status 0 == success.
        if isinstance(data, dict) and data.get("status", 0) not in (0, None):
            log.debug("Plaud non-zero status for %s: %s", path, data.get("msg"))
        return data

    # ---- auth ---------------------------------------------------------- #

    def check_auth(self) -> dict:
        """Validate the session via ``GET /user/me``. Raises on failure."""
        return self._get_json("/user/me")

    # ---- listing ------------------------------------------------------- #

    def list_files(
        self,
        skip: int = 0,
        limit: int = 200,
        include_trash: bool = False,
        sort_by: str = "start_time",
        is_desc: bool = True,
    ) -> FileListResponse:
        data = self._get_json(
            "/file/simple/web",
            skip=skip,
            limit=limit,
            is_trash=2 if include_trash else 0,
            sort_by=sort_by,
            is_desc=str(is_desc).lower(),
        )
        return FileListResponse.model_validate(data)

    def iter_files(self, include_trash: bool = False, page_size: int = 200) -> Iterator[PlaudFileDTO]:
        """Yield every file, paging through the list endpoint."""
        skip = 0
        while True:
            page = self.list_files(skip=skip, limit=page_size, include_trash=include_trash)
            if not page.data_file_list:
                break
            yield from page.data_file_list
            skip += len(page.data_file_list)
            if skip >= page.data_file_total or len(page.data_file_list) < page_size:
                break

    # ---- detail (transcript + summary) --------------------------------- #

    def get_detail(self, file_id: str) -> dict:
        """Full detail payload — includes the timestamped, speaker-labelled
        transcript and the template summary/notes."""
        return self._get_json(f"/file/detail/{file_id}")

    # ---- audio download ------------------------------------------------ #

    def resolve_audio_url(self, file: PlaudFileDTO, detail: dict | None = None) -> str | None:
        """Find the downloadable audio URL for a file.

        Strategy: inspect the file-detail payload for a URL-bearing field
        (the signed CDN/OSS URL the web player uses). The exact key is still
        being confirmed; we scan known hint keys at the top level and one
        level deep. Returns None if not found.
        """
        detail = detail if detail is not None else self.get_detail(file.id)

        def scan(obj: object) -> str | None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, str) and v.startswith("http") and (
                        k.lower() in _AUDIO_URL_HINTS or file.audio_ext in v or "audio" in k.lower()
                    ):
                        return v
                for v in obj.values():
                    found = scan(v)
                    if found:
                        return found
            elif isinstance(obj, list):
                for v in obj:
                    found = scan(v)
                    if found:
                        return found
            return None

        return scan(detail)

    def download_audio(self, file: PlaudFileDTO, dest: Path, detail: dict | None = None) -> Path:
        """Download the file's audio to ``dest``. Raises PlaudError if the
        download URL can't be resolved."""
        url = self.resolve_audio_url(file, detail=detail)
        if not url:
            raise PlaudError(
                f"Could not resolve an audio download URL for file {file.id}. "
                "The download endpoint is a known open question — see "
                "docs/plaud-api.md. Once confirmed, wire it in "
                "PlaudClient.resolve_audio_url / download_audio."
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Signed URLs are usually on a different host and need no auth headers.
        with httpx.Client(timeout=None, follow_redirects=True) as raw:
            with raw.stream("GET", url) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1 << 16):
                        fh.write(chunk)
        log.info("Downloaded %s -> %s (%d bytes)", file.id, dest, dest.stat().st_size)
        return dest
