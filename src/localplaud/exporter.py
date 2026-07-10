"""Export a recording to a single Markdown document.

Pulls everything localplaud knows about a file — cloud metadata, summaries,
and the speaker-labelled transcript — out of the database and renders it as
one self-contained ``.md``, suitable for archiving or dropping into a notes
app.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from .config import get_settings
from .db.models import PlaudFile, StageName
from .db.session import session_scope
from .store.speakers import display_names

log = logging.getLogger(__name__)


def _format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _format_duration(duration_ms: int) -> str:
    total = duration_ms // 1000
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes}m {seconds:02d}s"


def render_markdown(file_id: str) -> str:
    """Render one file's metadata, summaries, and transcript as Markdown.

    Raises :class:`ValueError` if the file isn't in the database.
    """
    with session_scope() as session:
        file = session.get(PlaudFile, file_id)
        if file is None:
            raise ValueError(f"no such file: {file_id}")

        parts: list[str] = [f"# {file.filename or file.id}", ""]

        meta: list[str] = []
        if file.start_time_ms is not None:
            recorded = datetime.fromtimestamp(file.start_time_ms / 1000, tz=UTC)
            meta.append(f"Recorded {recorded.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        if file.duration_ms is not None:
            meta.append(f"duration {_format_duration(file.duration_ms)}")
        if meta:
            parts += ["*" + " · ".join(meta) + "*", ""]

        settings = get_settings()
        independent = settings.pipeline.artifact_mode == "independent"
        stale_stages = {
            run.stage
            for run in file.stage_runs
            if (run.detail or {}).get("stale")
        }
        summaries = (
            [summary for summary in file.summaries if summary.source == "local"]
            if independent
            else file.summaries
        )
        summaries = [
            summary
            for summary in summaries
            if not (
                summary.source == "local"
                and (
                    (summary.template == "mind_map" and StageName.mind_map in stale_stages)
                    or (summary.template != "mind_map" and StageName.summarize in stale_stages)
                )
            )
        ]
        mind_maps = [s for s in summaries if s.template == "mind_map"]
        for summary in summaries:
            if summary.template == "mind_map":
                continue
            heading = f"## {summary.template}"
            if summary.title:
                heading += f": {summary.title}"
            parts += [heading, "", summary.content_md.strip(), ""]

        if mind_maps:
            outline = mind_maps[-1].content_md.strip()
            if outline.startswith("# "):
                # Demote the outline's H1 root beneath this section heading.
                outline = "###" + outline[1:]
            parts += ["## Mind map", "", outline, ""]

        # Corrected canonical transcript wins; the raw ASR row is the fallback.
        if independent:
            raw = file.local_transcript
        elif settings.pipeline.prefer_cloud_artifacts:
            raw = file.plaud_transcript or file.local_transcript
        else:
            raw = file.local_transcript
        corrected = (
            file.corrected_transcript_for_source(raw.source)
            if raw is not None
            else None
        )
        if corrected is not None:
            segments = corrected.segments
        else:
            segments = raw.segments if raw is not None else None
        if segments:
            names = display_names(session, file.id)
            parts += ["## Transcript", ""]
            for seg in segments:
                stamp = _format_timestamp(seg.get("start") or 0.0)
                speaker = seg.get("speaker")
                speaker = names.get(speaker, speaker) if speaker else None
                label = f"[{stamp}] {speaker}:" if speaker else f"[{stamp}]"
                text = (seg.get("text") or "").strip()
                parts.append(f"**{label}** {text}")
            parts.append("")

        return "\n".join(parts)


def export_to_file(file_id: str, dest: str | Path | None = None) -> Path:
    """Write the rendered Markdown to ``dest`` and return its path.

    Defaults to ``<download_dir>/<file_id>/export.md`` next to the audio.
    """
    content = render_markdown(file_id)
    if dest is None:
        dest = get_settings().poller.download_dir / file_id / "export.md"
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    log.info("exported %s to %s", file_id, dest)
    return dest
