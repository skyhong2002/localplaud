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


def _iter_strings(obj: object):
    """Yield every string value in a nested dict/list structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def _find_url(obj: object, must_contain: tuple[str, ...] = ()) -> str | None:
    """Find the first http(s) URL in ``obj``; if ``must_contain`` is given,
    prefer a URL containing any of those substrings, else fall back to any."""
    urls = [s for s in _iter_strings(obj) if s.startswith("http")]
    if must_contain:
        for u in urls:
            if any(m in u for m in must_contain):
                return u
    return urls[0] if urls else None


def _ext_from_url(url: str, default: str = "mp3") -> str:
    path = url.split("?", 1)[0]
    if "." in path.rsplit("/", 1)[-1]:
        return path.rsplit(".", 1)[-1].lower()
    return default


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

    def get_temp_url(self, file_id: str) -> str:
        """Resolve the signed, expiring media URL via ``GET /file/temp-url/{id}``.

        The response is a small JSON wrapper around a signed AWS S3 URL
        (host ``apse1-prod-plaud-bucket.s3.amazonaws.com``, path
        ``/audiofiles/{id}.mp3``). The exact wrapper key isn't documented, so
        we scan the payload for the signed URL. See docs/plaud-api.md."""
        data = self._get_json(f"/file/temp-url/{file_id}")
        url = _find_url(data, must_contain=(file_id, "amazonaws", "audiofiles", "Signature"))
        if not url:
            raise PlaudError(
                f"/file/temp-url/{file_id} returned no signed URL (payload keys: "
                f"{list(data)[:8] if isinstance(data, dict) else type(data).__name__})"
            )
        return url

    def download_audio(self, file: PlaudFileDTO, dest_dir: Path) -> Path:
        """Download the file's audio into ``dest_dir`` as ``audio.<ext>``.

        The real asset is typically MP3 (not the ``.opus`` the list metadata
        implies), so the extension is taken from the signed URL. Returns the
        written path."""
        url = self.get_temp_url(file.id)
        ext = _ext_from_url(url, default="mp3")
        dest = dest_dir / f"audio.{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Signed S3 URLs are on a different host and need no auth headers.
        with httpx.Client(timeout=None, follow_redirects=True) as raw, raw.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 16):
                    fh.write(chunk)
        log.info("Downloaded %s -> %s (%d bytes)", file.id, dest, dest.stat().st_size)
        return dest

    # ---- cloud-produced artifacts (optional reuse) --------------------- #

    def _fetch_gzip_asset(self, url: str) -> bytes:
        with httpx.Client(timeout=None, follow_redirects=True) as raw:
            resp = raw.get(url)
            resp.raise_for_status()
            data = resp.content
        if url.split("?", 1)[0].endswith(".gz"):
            import gzip

            return gzip.decompress(data)
        return data

    def get_cloud_summary_md(self, file_id: str, detail: dict | None = None) -> str | None:
        """Plaud's own summary (markdown), if present. Resolved from the signed
        ``file_summary/.../ai_content.md.gz`` asset in the detail payload."""
        detail = detail if detail is not None else self.get_detail(file_id)
        url = _find_url(detail, must_contain=("ai_content", "file_summary"))
        if not url:
            return None
        try:
            return self._fetch_gzip_asset(url).decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not fetch cloud summary for %s: %s", file_id, exc)
            return None

    def get_cloud_transcript_json(self, file_id: str, detail: dict | None = None) -> dict | None:
        """Plaud's own transcript as raw JSON (schema not yet modelled — see
        issue #9). Resolved from the ``file_transcript/.../trans_result.json.gz``
        signed asset."""
        detail = detail if detail is not None else self.get_detail(file_id)
        url = _find_url(detail, must_contain=("trans_result", "file_transcript"))
        if not url:
            return None
        try:
            import json

            return json.loads(self._fetch_gzip_asset(url))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not fetch cloud transcript for %s: %s", file_id, exc)
            return None
