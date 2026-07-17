"""Revocable public read-only recording share links."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..db.models import PlaudFile, ShareLink, StageName
from ..db.session import session_scope
from ..i18n import catalog, translator
from ..markdown import render_markdown
from ..preferences import get_workspace_preferences
from ..store.speakers import display_names
from .media import audio_file_response

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["markdown"] = render_markdown

_SPEAKER_COLORS = (
    "#F14349",
    "#007AFF",
    "#16A34A",
    "#8F53ED",
    "#0891B2",
    "#DB2777",
    "#CA8A04",
    "#EA580C",
)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not Found")


def _visible_recording(session, file_id: str) -> PlaudFile:
    recording = session.get(PlaudFile, file_id)
    if recording is None or recording.is_trash:
        raise _not_found()
    return recording


def _active_link(session, file_id: str) -> ShareLink | None:
    return session.scalar(
        select(ShareLink)
        .where(ShareLink.file_id == file_id, ShareLink.revoked_at.is_(None))
        .order_by(ShareLink.created_at.desc())
        .limit(1)
    )


def _public_link(session, token: str) -> ShareLink:
    link = session.scalar(
        select(ShareLink)
        .join(PlaudFile, PlaudFile.id == ShareLink.file_id)
        .where(
            ShareLink.token == token,
            ShareLink.revoked_at.is_(None),
            PlaudFile.is_trash.is_(False),
        )
    )
    if link is None:
        raise _not_found()
    return link


def _url(request: Request, token: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/share/{token}"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


def _link_payload(request: Request, link: ShareLink | None) -> dict:
    return {
        "active": link is not None,
        "url": _url(request, link.token) if link is not None else None,
        "created_at": _iso(link.created_at) if link is not None else None,
        "last_used_at": _iso(link.last_used_at) if link is not None else None,
    }


def _recorded_at(start_time_ms: int | None, preferences: dict) -> str:
    if not start_time_ms:
        return ""
    value = datetime.fromtimestamp(start_time_ms / 1000, tz=UTC).astimezone(
        ZoneInfo(preferences["timezone"])
    )
    if preferences["locale"] == "zh-Hant-TW":
        pattern = "%Y年%m月%d日 · %I:%M %p" if preferences["hour_cycle"] == "12" else "%Y年%m月%d日 · %H:%M"
    else:
        pattern = "%b %d, %Y · %I:%M %p" if preferences["hour_cycle"] == "12" else "%b %d, %Y · %H:%M"
    return value.strftime(pattern)


def _duration(duration_ms: int | None) -> str:
    seconds = int((duration_ms or 0) // 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


@router.post("/api/files/{file_id}/share-link")
def create_share_link(request: Request, file_id: str) -> dict:
    with session_scope() as session:
        _visible_recording(session, file_id)
        link = _active_link(session, file_id)
        if link is None:
            link = ShareLink(
                id=str(uuid.uuid4()),
                file_id=file_id,
                token=secrets.token_urlsafe(32),
            )
            session.add(link)
            session.flush()
        return _link_payload(request, link)


@router.get("/api/files/{file_id}/share-link")
def get_share_link(request: Request, file_id: str) -> dict:
    with session_scope() as session:
        _visible_recording(session, file_id)
        return _link_payload(request, _active_link(session, file_id))


@router.delete("/api/files/{file_id}/share-link")
def revoke_share_link(file_id: str) -> dict:
    with session_scope() as session:
        _visible_recording(session, file_id)
        link = _active_link(session, file_id)
        if link is not None:
            link.revoked_at = datetime.now(UTC)
    return {"ok": True}


@router.get("/share/{token}", response_class=HTMLResponse)
def public_share(request: Request, token: str):
    with session_scope() as session:
        link = _public_link(session, token)
        recording = link.file
        raw_transcript = recording.local_transcript
        corrected = recording.corrected_transcript_for_source("local")
        segments = list(
            corrected.segments
            if corrected is not None
            else raw_transcript.segments
            if raw_transcript is not None
            else []
        )
        names = display_names(session, recording.id)
        speaker_colors: dict[str, str] = {}
        for segment in segments:
            speaker = segment.get("speaker")
            if speaker and speaker not in speaker_colors:
                speaker_colors[speaker] = _SPEAKER_COLORS[len(speaker_colors) % len(_SPEAKER_COLORS)]
        stale_stages = {
            run.stage for run in recording.stage_runs if (run.detail or {}).get("stale")
        }
        notes = [
            summary
            for summary in sorted(recording.summaries, key=lambda item: (item.template, item.id))
            if summary.source == "local"
            and summary.input_transcript_source not in {"cloud", "plaud"}
            and not (
                (summary.template == "mind_map" and StageName.mind_map in stale_stages)
                or (summary.template != "mind_map" and StageName.summarize in stale_stages)
            )
        ]
        preferences = get_workspace_preferences(session)
        link.last_used_at = datetime.now(UTC)
        context = {
            "recording": {
                "title": recording.display_title,
                "recorded_at": _recorded_at(recording.start_time_ms, preferences),
                "duration": _duration(recording.duration_ms),
                "has_audio": bool(recording.audio_path and Path(recording.audio_path).exists()),
            },
            "segments": segments,
            "speaker_names": names,
            "speaker_colors": speaker_colors,
            "notes": notes,
            "audio_url": f"/share/{token}/audio",
            "workspace_preferences": preferences,
            "translations": catalog(preferences["locale"]),
            "t": translator(preferences["locale"]),
        }
    return templates.TemplateResponse(request=request, name="share.html", context=context)


@router.get("/share/{token}/audio")
def public_share_audio(token: str):
    with session_scope() as session:
        link = _public_link(session, token)
        path = link.file.audio_path
    if not path or not Path(path).is_file():
        raise _not_found()
    return audio_file_response(path)
