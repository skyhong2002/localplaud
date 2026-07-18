"""Read-only client for the official Plaud Open API (platform.plaud.ai).

This is the sanctioned counterpart to the reverse-engineered ``PlaudClient``:
OAuth-authenticated (auto-refreshing, see ``oauth.py``) and stable. Endpoints
(all GET; localplaud never mutates cloud data):

- ``GET /open/third-party/users/current``            — auth validation
- ``GET /open/third-party/files/?page=&page_size=``  — file list (paged)
- ``GET /open/third-party/files/{id}``               — detail: 24h presigned
  audio URL + ``source_list`` (transcript) + ``note_list`` (summary markdown)

The Open API intentionally exposes only minimal recording metadata plus raw audio
and optional migration artifacts.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
from tenacity import retry, retry_if_not_exception_type, stop_after_attempt, wait_exponential

from ..config import PlaudOfficialConfig
from .common import (
    _MAX_AUDIO_BYTES,
    PlaudAuthError,
    PlaudError,
    _assert_safe_fetch_url,
    _ext_from_url,
)
from .models import PlaudFileDTO
from .oauth import OAuthError, OfficialTokenStore

log = logging.getLogger(__name__)


class PlaudRateLimited(PlaudError):
    """HTTP 429 from the Open API — transient; retried with backoff."""

# The Open API caps page_size at 100 (page at 1000).
_PAGE_SIZE = 100

# source_list / note_list entry types we consume.
_TRANSCRIPT_TYPE = "transaction"
_SUMMARY_TYPE = "auto_sum_note"


def _normalize_cloud_markdown(markdown: str) -> str:
    """Keep Plaud rule lines from being parsed as setext headings."""
    normalized: list[str] = []
    for line in markdown.splitlines():
        if re.fullmatch(r"---[ \t]*", line) and normalized and normalized[-1].strip():
            normalized.append("")
        normalized.append(line)
    return "\n".join(normalized).strip()


def _outline_markdown(raw: str) -> str | None:
    """Render Plaud's chapter outline JSON as a timestamped Markdown list."""
    try:
        topics = json.loads(raw)
    except json.JSONDecodeError:
        return None
    lines = []
    for topic in topics if isinstance(topics, list) else []:
        text = str(topic.get("topic") or "").strip() if isinstance(topic, dict) else ""
        if not text:
            continue
        start = int(topic.get("start_time") or 0) // 1000
        lines.append(f"- [{start // 60}:{start % 60:02d}] {text}")
    return "\n".join(lines) or None


def _cloud_notes(detail: dict) -> list[dict]:
    notes = []
    for item in detail.get("note_list") or []:
        markdown = item.get("data_content")
        if not markdown:
            continue
        markdown = _normalize_cloud_markdown(str(markdown))
        if not markdown:
            continue
        heading = re.search(r"^# (.+?)\s*$", markdown, flags=re.MULTILINE)
        title = heading.group(1).strip() if heading else None
        notes.append(
            {
                "key": str(item.get("data_type") or ""),
                "title": title or str(item.get("data_title") or "").strip() or None,
                "markdown": markdown,
                "assets": (
                    dict(item["download_link_map"])
                    if isinstance(item.get("download_link_map"), dict)
                    else {}
                ),
            }
        )
    # Plaud presents its chapter outline alongside notes; mirror it as one.
    outline_raw = next(
        (
            s.get("data_content")
            for s in detail.get("source_list") or []
            if s.get("data_type") == "outline" and s.get("data_content")
        ),
        None,
    )
    if outline_raw:
        outline_md = _outline_markdown(str(outline_raw))
        if outline_md:
            notes.append(
                {"key": "outline", "title": "Outline", "markdown": outline_md, "assets": {}}
            )
    return notes


def _transcript_from_source_list(source_list: list, *, context: str) -> list[dict] | None:
    """Normalize Plaud's ``transaction`` source entry to local segment shape.

    Entries carry a JSON string of ``{content, start_time, end_time, speaker,
    original_speaker}`` objects with millisecond times; the local shape is
    ``{text, start, end, speaker}`` in seconds.
    """
    raw = next(
        (
            s.get("data_content")
            for s in source_list
            if isinstance(s, dict) and s.get("data_type") == _TRANSCRIPT_TYPE
        ),
        None,
    )
    if not raw:
        return None
    try:
        segments = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("Unparsable cloud transcript for %s: %s", context, exc)
        return None
    out = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        out.append(
            {
                "text": (seg.get("content") or "").strip(),
                "start": (seg.get("start_time") or 0) / 1000.0,
                "end": (seg.get("end_time") or 0) / 1000.0,
                "speaker": seg.get("speaker") or seg.get("original_speaker"),
            }
        )
    return out or None


def _parse_iso_ms(value: str | None) -> int | None:
    """Open API timestamps are naive ISO strings in UTC → epoch ms."""
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)
    except ValueError:
        return None


def _to_dto(item: dict) -> PlaudFileDTO:
    """Normalize an Open API file object onto the shared DTO. Fields the Open
    API doesn't expose (version/file_md5/edit_time/is_trash/scene) stay None —
    the poller treats those as "unknown", not "unchanged"."""
    duration = item.get("duration")
    start = _parse_iso_ms(item.get("start_at"))
    dur_ms = int(duration) if duration not in (None, "") else None
    return PlaudFileDTO(
        id=item["id"],
        filename=item.get("name") or "",
        duration=dur_ms,
        start_time=start,
        end_time=start + dur_ms if start is not None and dur_ms is not None else None,
        **{k: v for k, v in item.items() if k not in
           ("id", "name", "duration", "start_at", "created_at")},
    )


class PlaudOfficialClient:
    def __init__(self, cfg: PlaudOfficialConfig):
        self.cfg = cfg
        self.tokens = OfficialTokenStore(
            tokens_path=cfg.tokens_path,
            refresh_url=cfg.refresh_url,
            timeout=cfg.request_timeout_seconds,
        )
        self._client = httpx.Client(
            base_url=cfg.api_base.rstrip("/"),
            headers={"Accept": "application/json"},
            timeout=cfg.request_timeout_seconds,
        )
        # Detail payloads are reused across download + transcript + summary in
        # one poll cycle; cache the latest few to avoid triple-fetching.
        self._detail_cache: dict[str, dict] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PlaudOfficialClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- low level ------------------------------------------------------- #

    # Transient transport errors AND rate limiting retry with backoff (the
    # Open API 429s under a burst of detail calls, e.g. a first-sync backlog);
    # auth failures and other 4xx/5xx don't get better on retry.
    @retry(
        stop=stop_after_attempt(6),
        wait=wait_exponential(multiplier=2, max=60),
        retry=retry_if_not_exception_type((PlaudAuthError, httpx.HTTPStatusError)),
        reraise=True,
    )
    def _get(self, path: str, **params) -> httpx.Response:
        try:
            token = self.tokens.get_access_token()
        except OAuthError as exc:
            raise PlaudAuthError(str(exc)) from exc
        resp = self._client.get(
            path, params=params or None, headers={"Authorization": f"Bearer {token}"}
        )
        if resp.status_code == 401:
            # Stale token despite the expiry check (revoked/clock skew) —
            # force one refresh and retry before giving up.
            try:
                token = self.tokens.get_access_token(force_refresh=True)
            except OAuthError as exc:
                raise PlaudAuthError(str(exc)) from exc
            resp = self._client.get(
                path, params=params or None, headers={"Authorization": f"Bearer {token}"}
            )
        if resp.status_code == 429:
            raise PlaudRateLimited(f"Open API rate limit hit for {path}")
        if resp.status_code in (401, 403):
            raise PlaudAuthError(
                f"Plaud Open API returned {resp.status_code} for {path} — "
                "run `localplaud auth login` to sign in again."
            )
        resp.raise_for_status()
        return resp

    def _get_json(self, path: str, **params) -> dict:
        return self._get(path, **params).json()

    # ---- auth ------------------------------------------------------------ #

    def check_auth(self) -> dict:
        """Validate the session via ``GET /open/third-party/users/current``."""
        return self._get_json("/open/third-party/users/current")

    # ---- listing ----------------------------------------------------------#

    def iter_files(self, include_trash: bool = False, page_size: int = _PAGE_SIZE) -> Iterator[PlaudFileDTO]:
        """Yield every file, paging through the list endpoint.

        ``include_trash`` is accepted for interface parity but the Open API
        does not expose trashed files at all."""
        page = 1
        page_size = min(page_size, _PAGE_SIZE)
        while True:
            data = self._get_json(
                "/open/third-party/files/", page=page, page_size=page_size
            )
            items = data.get("data") or []
            for item in items:
                yield _to_dto(item)
            if len(items) < page_size:
                break
            page += 1

    # ---- detail (audio URL + transcript + summary) ------------------------ #

    def get_detail(self, file_id: str) -> dict:
        """Full detail payload: ``presigned_url`` (24h), ``source_list``
        (transcript segments), ``note_list`` (summary markdown)."""
        cached = self._detail_cache.get(file_id)
        if cached is not None:
            return cached
        detail = self._get_json(f"/open/third-party/files/{file_id}")
        if len(self._detail_cache) > 64:
            self._detail_cache.clear()
        self._detail_cache[file_id] = detail
        return detail

    # ---- audio download ---------------------------------------------------#

    def download_audio(self, file: PlaudFileDTO, dest_dir: Path) -> Path:
        """Download the file's audio via the 24h presigned URL from the detail
        payload into ``dest_dir`` as ``audio.<ext>``."""
        detail = self.get_detail(file.id)
        url = detail.get("presigned_url")
        if not url:
            raise PlaudError(f"Open API returned no presigned_url for {file.id}")
        _assert_safe_fetch_url(url)
        ext = _ext_from_url(url, default="mp3")
        dest = dest_dir / f"audio.{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        # Presigned S3 URLs are on a different host and need no auth headers;
        # follow_redirects stays off so a redirect can't bounce us to an
        # internal host after the safety check.
        with httpx.Client(timeout=120, follow_redirects=False) as raw, raw.stream(
            "GET", url
        ) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 16):
                    written += len(chunk)
                    if written > _MAX_AUDIO_BYTES:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        raise PlaudError(
                            f"audio for {file.id} exceeds {_MAX_AUDIO_BYTES} bytes; aborting"
                        )
                    fh.write(chunk)
        log.info("Downloaded %s -> %s (%d bytes)", file.id, dest, written)
        return dest

    # ---- cloud-produced artifacts (mirroring Plaud's own work) ------------ #

    def get_cloud_summary_md(self, file_id: str, detail: dict | None = None) -> str | None:
        """Plaud's own summary (markdown), from the ``auto_sum_note`` entry of
        the detail payload's ``note_list``."""
        notes = self.get_cloud_notes(file_id, detail)
        summary = next((note for note in notes if note["key"] == _SUMMARY_TYPE), None)
        if summary is None:
            summary = next(
                (note for note in notes if "sum" in note["key"].casefold()),
                notes[0] if notes else None,
            )
        return summary["markdown"] if summary is not None else None

    def get_cloud_notes(self, file_id: str, detail: dict | None = None) -> list[dict]:
        """Return every Plaud note with explicit cloud provenance metadata."""
        detail = detail if detail is not None else self.get_detail(file_id)
        return _cloud_notes(detail)

    def get_cloud_transcript_segments(
        self, file_id: str, detail: dict | None = None
    ) -> list[dict] | None:
        """Plaud's own transcript, normalized to the local segment shape
        (``{text, start, end, speaker}``, times in seconds). The Open API
        serves it as a JSON string of ``{content, start_time, end_time,
        speaker, original_speaker}`` objects (times in ms) inside the
        ``transaction`` entry of ``source_list``."""
        detail = detail if detail is not None else self.get_detail(file_id)
        return _transcript_from_source_list(
            detail.get("source_list") or [], context=file_id
        )
