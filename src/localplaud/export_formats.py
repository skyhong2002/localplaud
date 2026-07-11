"""Portable recording exports for the Web App."""

from __future__ import annotations

from .config import get_settings
from .db.models import PlaudFile, StageName
from .db.session import session_scope
from .store.speakers import display_names


def _stamp(seconds: float, *, milliseconds: bool = False) -> str:
    milliseconds_total = max(0, round(float(seconds or 0) * 1000))
    hours, remainder = divmod(milliseconds_total, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    if milliseconds:
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _recording_data(file_id: str) -> dict:
    with session_scope() as session:
        file = session.get(PlaudFile, file_id)
        if file is None:
            raise ValueError(f"no such file: {file_id}")
        settings = get_settings()
        if settings.pipeline.artifact_mode == "independent":
            raw = file.local_transcript
        elif settings.pipeline.prefer_cloud_artifacts:
            raw = file.plaud_transcript or file.local_transcript
        else:
            raw = file.local_transcript
        corrected = file.corrected_transcript_for_source(raw.source) if raw else None
        segments = list((corrected.segments if corrected else raw.segments) or []) if raw else []
        names = display_names(session, file.id)
        stale = {
            run.stage for run in file.stage_runs if (run.detail or {}).get("stale")
        }
        summaries = [
            summary
            for summary in file.summaries
            if (settings.pipeline.artifact_mode != "independent" or summary.source == "local")
            and summary.template != "mind_map"
            and not (
                summary.source == "local"
                and StageName.summarize in stale
            )
        ]
        notes = [
            {
                "title": (summary.template_snapshot or {}).get("name")
                or summary.title
                or summary.template.replace("-", " ").title(),
                "content": summary.content_md,
            }
            for summary in summaries
        ] + [
            {"title": note.title, "content": note.content_md} for note in file.user_notes
        ]
        return {
            "title": file.filename or file.id,
            "segments": segments,
            "speaker_names": names,
            "notes": notes,
            "audio_path": file.audio_path,
        }


def render_transcript(
    file_id: str,
    fmt: str,
    *,
    timestamps: bool = True,
    speakers: bool = True,
) -> tuple[bytes, str]:
    data = _recording_data(file_id)
    segments = data["segments"]
    if not segments:
        raise LookupError("recording has no exportable transcript")

    def text_for(segment: dict) -> str:
        text = str(segment.get("text") or "").strip()
        speaker = segment.get("speaker")
        if speakers and speaker:
            text = f"{data['speaker_names'].get(speaker, speaker)}: {text}"
        return text

    if fmt in {"srt", "vtt"}:
        blocks = []
        for index, segment in enumerate(segments, 1):
            start = _stamp(segment.get("start") or 0, milliseconds=True)
            end = _stamp(segment.get("end") or segment.get("start") or 0, milliseconds=True)
            if fmt == "vtt":
                start, end = start.replace(",", "."), end.replace(",", ".")
            blocks.append(f"{index}\n{start} --> {end}\n{text_for(segment)}")
        prefix = "WEBVTT\n\n" if fmt == "vtt" else ""
        return (prefix + "\n\n".join(blocks) + "\n").encode(), f"text/{fmt}"

    lines = []
    for segment in segments:
        prefix = f"[{_stamp(segment.get('start') or 0)}] " if timestamps else ""
        lines.append(prefix + text_for(segment))
    title = data["title"]
    if fmt == "txt":
        return (f"{title}\n\n" + "\n".join(lines) + "\n").encode(), "text/plain"
    raise ValueError("unsupported transcript format")


def render_notes(file_id: str, fmt: str) -> tuple[bytes, str]:
    data = _recording_data(file_id)
    if not data["notes"]:
        raise LookupError("recording has no exportable notes")
    lines: list[str] = []
    markdown: list[str] = [f"# {data['title']}", ""]
    for note in data["notes"]:
        lines += [note["title"], note["content"], ""]
        markdown += [f"## {note['title']}", "", note["content"].strip(), ""]
    if fmt == "md":
        return "\n".join(markdown).encode(), "text/markdown"
    if fmt == "txt":
        return (data["title"] + "\n\n" + "\n".join(lines)).encode(), "text/plain"
    raise ValueError("unsupported notes format")
